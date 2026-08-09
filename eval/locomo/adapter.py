"""Strict LoCoMo output importer for the unified evidence core."""

from __future__ import annotations

import json
import math
from collections import Counter
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

from .models import LocomoImportSpec

_IMPLEMENTATION = "eval.locomo.adapter:LocomoEvidenceAdapter"


class LocomoEvidenceAdapter:
    identity = AdapterIdentity(adapter_id="locomo-import", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        spec = LocomoImportSpec.model_validate_json(context.instruction)
        self._validate_contract(context, spec)
        started_at = datetime.now(UTC)

        raw, raw_ref = await import_workspace_file(
            context.artifacts,
            context.workspace,
            spec.results_path,
            "application/vnd.locomo.results+json",
            max_bytes=spec.max_artifact_bytes,
        )
        records = self._parse_results(raw, spec)

        artifact_refs = [raw_ref]
        metrics_ref = None
        if spec.metrics_path is not None:
            metrics_raw, metrics_ref = await import_workspace_file(
                context.artifacts,
                context.workspace,
                spec.metrics_path,
                "application/vnd.locomo.metrics+json",
                max_bytes=spec.max_artifact_bytes,
            )
            self._validate_metrics(metrics_raw)
            artifact_refs.append(metrics_ref)
        if spec.report_path is not None:
            _, report_ref = await import_workspace_file(
                context.artifacts,
                context.workspace,
                spec.report_path,
                "text/html",
                max_bytes=spec.max_artifact_bytes,
            )
            artifact_refs.append(report_ref)

        trajectory_refs = []
        for path in spec.trajectory_paths:
            _, reference = await import_workspace_file(
                context.artifacts,
                context.workspace,
                path,
                "application/x-ndjson",
                max_bytes=spec.max_artifact_bytes,
            )
            trajectory_refs.append(reference)

        passed = sum(1 for record in records if record["pass"])
        pass_rate = passed / len(records)
        fatal = [record for record in records if record["status"] in spec.fatal_statuses]
        q_types = Counter(record["q_type"] for record in records)
        outcome = {
            "schema_version": "eval.locomo-outcome.v1",
            "raw_results_digest": raw_ref.digest,
            "metrics_digest": metrics_ref.digest if metrics_ref is not None else None,
            "qa_count": len(records),
            "pass_count": passed,
            "failure_count": len(records) - passed,
            "fatal_count": len(fatal),
            "pass_rate": pass_rate,
            "q_type_counts": dict(sorted(q_types.items())),
        }
        outcome_ref = await context.artifacts.put_artifact(
            canonical_json_bytes(outcome),
            "application/vnd.cc-harness.locomo-outcome+json",
        )
        grader_results = tuple(
            GraderResult(
                grader_id=grader.grader_id,
                status=(
                    ResultStatus.PASS
                    if pass_rate >= grader.success_threshold and not fatal
                    else ResultStatus.FAIL
                ),
                score=pass_rate,
                message=(
                    f"LoCoMo passed {passed}/{len(records)} QA records; fatal records={len(fatal)}"
                ),
                details_ref=outcome_ref,
            )
            for grader in context.request.task.graders
        )
        status = (
            ResultStatus.PASS
            if all(result.status is ResultStatus.PASS for result in grader_results)
            else ResultStatus.FAIL
        )
        metric_values = [
            MetricValue(name="qa-count", value=float(len(records)), unit="questions"),
            MetricValue(name="pass-rate", value=pass_rate, unit="ratio"),
            MetricValue(name="fatal-count", value=float(len(fatal)), unit="questions"),
        ]
        for q_type in sorted(q_types):
            subset = [record for record in records if record["q_type"] == q_type]
            subset_rate = sum(1 for record in subset if record["pass"]) / len(subset)
            safe_q_type = "".join(
                char if char.isascii() and char.isalnum() else "-" for char in q_type.lower()
            )
            safe_q_type = safe_q_type.strip("-") or "unknown"
            metric_values.append(
                MetricValue(name=f"q-type-{safe_q_type}-pass-rate", value=subset_rate, unit="ratio")
            )

        common = {
            "trial_id": context.request.trial_id,
            "run_id": context.request.run_id,
            "run_manifest_digest": context.request.run_manifest_digest,
            "task_id": context.request.task.task_id,
            "task_contract_digest": content_fingerprint(context.request.task),
            "attempt": context.attempt,
            "adapter": context.request.adapter,
            "started_at": started_at,
            "finished_at": datetime.now(UTC),
            "usage": self._usage(records),
            "grader_results": grader_results,
            "metrics": tuple(metric_values),
            "outcome_ref": outcome_ref,
            "trajectory_ref": trajectory_refs[0] if trajectory_refs else None,
            "artifacts": unique_artifacts(*artifact_refs, *trajectory_refs[1:]),
        }
        if status is ResultStatus.PASS:
            return TrialResult(**common, status=status)
        return TrialResult(
            **common,
            status=status,
            failure=FailureRecord(
                category="locomo-regression",
                message=(
                    f"LoCoMo pass rate {pass_rate:.4f} did not satisfy all graders; "
                    f"fatal records={len(fatal)}"
                ),
                evidence_ref=outcome_ref,
            ),
        )

    @staticmethod
    def _validate_contract(context: TrialExecutionContext, spec: LocomoImportSpec) -> None:
        if spec.contract_id != context.request.task.task_id:
            raise EvidenceIntegrityError("LoCoMo instruction does not match the Task Contract")
        if context.request.adapter != LocomoEvidenceAdapter.identity:
            raise EvidenceIntegrityError("LoCoMo request declares the wrong adapter identity")
        if any(grader.implementation != _IMPLEMENTATION for grader in context.request.task.graders):
            raise EvidenceIntegrityError("LoCoMo task declares an unsupported grader")

    @staticmethod
    def _parse_results(raw: bytes, spec: LocomoImportSpec) -> list[dict[str, Any]]:
        try:
            records = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameworkArtifactError("LoCoMo results are not valid JSON") from exc
        if not isinstance(records, list) or len(records) < spec.minimum_qa_count:
            raise FrameworkArtifactError(
                f"LoCoMo result count is below the contract minimum: {len(records) if isinstance(records, list) else 0}"
            )

        parsed = []
        present_q_types = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict) or not isinstance(record.get("pass"), bool):
                raise FrameworkArtifactError(f"LoCoMo result {index} lacks a boolean pass")
            status = record.get("status")
            if not isinstance(status, str) or not status:
                raise FrameworkArtifactError(f"LoCoMo result {index} lacks a status")
            q_type = str(record.get("q_type", "unknown"))
            present_q_types.add(q_type)
            for key in ("prompt_tokens", "completion_tokens"):
                value = record.get(key, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise FrameworkArtifactError(f"LoCoMo result {index} has invalid {key}")
            cost = record.get("cost_usd", 0.0)
            if (
                not isinstance(cost, (int, float))
                or isinstance(cost, bool)
                or not math.isfinite(float(cost))
                or cost < 0
            ):
                raise FrameworkArtifactError(f"LoCoMo result {index} has invalid cost_usd")
            parsed.append(
                {
                    **record,
                    "status": status,
                    "q_type": q_type,
                    "cost_usd": float(cost),
                }
            )
        missing = set(spec.required_q_types) - present_q_types
        if missing:
            raise FrameworkArtifactError(f"LoCoMo results are missing q types: {sorted(missing)}")
        return parsed

    @staticmethod
    def _validate_metrics(raw: bytes) -> None:
        try:
            metrics = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameworkArtifactError("LoCoMo metrics are not valid JSON") from exc
        if not isinstance(metrics, dict):
            raise FrameworkArtifactError("LoCoMo metrics must be a JSON object")

    @staticmethod
    def _usage(records: list[dict[str, Any]]) -> ResourceUsage:
        return ResourceUsage(
            wall_time_ms=0,
            steps=len(records),
            model_calls=sum(
                1
                for record in records
                if record.get("prompt_tokens", 0) or record.get("completion_tokens", 0)
            ),
            tool_calls=sum(len(record.get("tool_calls") or []) for record in records),
            input_tokens=sum(record.get("prompt_tokens", 0) for record in records),
            output_tokens=sum(record.get("completion_tokens", 0) for record in records),
            cost_microusd=round(sum(record.get("cost_usd", 0.0) for record in records) * 1_000_000),
        )
