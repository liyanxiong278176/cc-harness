"""Strict SWE-bench Verified prediction and report importer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from eval.core import (
    AdapterIdentity,
    FailureRecord,
    GraderResult,
    MetricValue,
    ResourceUsage,
    ResultStatus,
    TrialExecutionContext,
    TrialResult,
    canonical_json_bytes,
    content_fingerprint,
)
from eval.core.framework_artifacts import (
    FrameworkArtifactError,
    import_workspace_file,
    unique_artifacts,
)
from eval.core.store import EvidenceIntegrityError

from .models import SwebenchImportSpec

_IMPLEMENTATION = "eval.swebench.adapter:SwebenchEvidenceAdapter"


class SwebenchEvidenceAdapter:
    identity = AdapterIdentity(adapter_id="swebench-import", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        spec = SwebenchImportSpec.model_validate_json(context.instruction)
        self._validate_contract(context, spec)
        started_at = datetime.now(UTC)
        prediction_raw, prediction_ref = await import_workspace_file(
            context.artifacts,
            context.workspace,
            spec.prediction_path,
            "application/x-ndjson",
            max_bytes=spec.max_artifact_bytes,
        )
        report_raw, report_ref = await import_workspace_file(
            context.artifacts,
            context.workspace,
            spec.report_path,
            "application/vnd.swebench.report+json",
            max_bytes=spec.max_artifact_bytes,
        )
        prediction = self._parse_prediction(prediction_raw, spec.instance_id)
        report = self._parse_report(report_raw, spec.instance_id)
        refs = [prediction_ref, report_ref]
        for path in spec.log_paths:
            _, reference = await import_workspace_file(
                context.artifacts,
                context.workspace,
                path,
                "text/plain",
                max_bytes=spec.max_artifact_bytes,
            )
            refs.append(reference)
        trajectory_ref = None
        if spec.trajectory_path:
            _, trajectory_ref = await import_workspace_file(
                context.artifacts,
                context.workspace,
                spec.trajectory_path,
                "application/x-ndjson",
                max_bytes=spec.max_artifact_bytes,
            )
        resolved = report["resolved"]
        outcome_ref = await context.artifacts.put_artifact(
            canonical_json_bytes(
                {
                    "schema_version": "eval.swebench-outcome.v1",
                    "instance_id": spec.instance_id,
                    "prediction_digest": prediction_ref.digest,
                    "report_digest": report_ref.digest,
                    "patch_digest": prediction["patch_digest"],
                    **report,
                }
            ),
            "application/vnd.cc-harness.swebench-outcome+json",
        )
        status = ResultStatus.PASS if resolved else ResultStatus.FAIL
        grader_results = tuple(
            GraderResult(
                grader_id=grader.grader_id,
                status=status,
                score=1.0 if resolved else 0.0,
                message=f"SWE-bench instance resolved={str(resolved).lower()}",
                details_ref=outcome_ref,
            )
            for grader in context.request.task.graders
        )
        common = {
            "trial_id": context.request.trial_id,
            "run_id": context.request.run_id,
            "run_manifest_digest": context.request.run_manifest_digest,
            "task_id": context.request.task.task_id,
            "task_contract_digest": content_fingerprint(context.request.task),
            "attempt": context.attempt,
            "adapter": context.request.adapter,
            "status": status,
            "started_at": started_at,
            "finished_at": datetime.now(UTC),
            "usage": ResourceUsage(
                wall_time_ms=0,
                steps=1,
                model_calls=0,
                tool_calls=0,
                input_tokens=0,
                output_tokens=0,
                cost_microusd=0,
            ),
            "grader_results": grader_results,
            "metrics": (
                MetricValue(name="resolved", value=1.0 if resolved else 0.0, unit="boolean"),
            ),
            "outcome_ref": outcome_ref,
            "trajectory_ref": trajectory_ref,
            "artifacts": unique_artifacts(*refs),
        }
        if resolved:
            return TrialResult(**common)
        return TrialResult(
            **common,
            failure=FailureRecord(
                category="swebench-unresolved",
                message="SWE-bench verifier did not fully resolve the instance",
                evidence_ref=outcome_ref,
            ),
        )

    @staticmethod
    def _validate_contract(context: TrialExecutionContext, spec: SwebenchImportSpec) -> None:
        if spec.contract_id != context.request.task.task_id:
            raise EvidenceIntegrityError("SWE-bench instruction does not match the Task Contract")
        if context.request.adapter != SwebenchEvidenceAdapter.identity:
            raise EvidenceIntegrityError("SWE-bench request declares the wrong adapter identity")
        if any(grader.implementation != _IMPLEMENTATION for grader in context.request.task.graders):
            raise EvidenceIntegrityError("SWE-bench task declares an unsupported grader")

    @staticmethod
    def _parse_prediction(raw: bytes, instance_id: str) -> dict[str, str]:
        try:
            text = raw.decode("utf-8")
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameworkArtifactError("SWE-bench predictions are not valid JSONL") from exc
        matches = [
            item
            for item in records
            if isinstance(item, dict) and item.get("instance_id") == instance_id
        ]
        if len(matches) != 1 or len(records) != 1:
            raise FrameworkArtifactError(
                "SWE-bench prediction must contain exactly the contracted instance"
            )
        prediction = matches[0]
        if prediction.get("model_name_or_path") != "deepseek-v4-flash":
            raise FrameworkArtifactError("SWE-bench prediction reports the wrong model")
        patch = prediction.get("model_patch")
        if not isinstance(patch, str):
            raise FrameworkArtifactError("SWE-bench prediction lacks model_patch")
        import hashlib

        return {"patch_digest": f"sha256:{hashlib.sha256(patch.encode()).hexdigest()}"}

    @staticmethod
    def _parse_report(raw: bytes, instance_id: str) -> dict[str, Any]:
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameworkArtifactError("SWE-bench report is not valid JSON") from exc
        if not isinstance(document, dict) or set(document) != {instance_id}:
            raise FrameworkArtifactError(
                "SWE-bench report must contain exactly the contracted instance"
            )
        report = document[instance_id]
        required = ("patch_is_None", "patch_exists", "patch_successfully_applied", "resolved")
        if not isinstance(report, dict) or any(
            not isinstance(report.get(key), bool) for key in required
        ):
            raise FrameworkArtifactError("SWE-bench report has an unsupported schema")
        if report["resolved"] and not report["patch_successfully_applied"]:
            raise FrameworkArtifactError("resolved report claims the patch was not applied")
        return {key: report[key] for key in required}
