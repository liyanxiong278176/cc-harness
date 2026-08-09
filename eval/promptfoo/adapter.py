"""Strict Promptfoo output importer for the unified evidence core."""

from __future__ import annotations

import json
import math
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

from .models import PromptfooImportSpec

_IMPLEMENTATION = "eval.promptfoo.adapter:PromptfooEvidenceAdapter"


class PromptfooEvidenceAdapter:
    identity = AdapterIdentity(adapter_id="promptfoo-import", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        spec = PromptfooImportSpec.model_validate_json(context.instruction)
        self._validate_contract(context, spec)
        started_at = datetime.now(UTC)

        raw, raw_ref = await import_workspace_file(
            context.artifacts,
            context.workspace,
            spec.results_path,
            "application/vnd.promptfoo.results+json",
            max_bytes=spec.max_artifact_bytes,
        )
        records = self._parse_results(raw, spec.minimum_test_count)

        extra_refs = []
        for path in spec.artifact_paths:
            _, reference = await import_workspace_file(
                context.artifacts,
                context.workspace,
                path,
                self._media_type(path),
                max_bytes=spec.max_artifact_bytes,
            )
            extra_refs.append(reference)

        trajectory_ref = None
        if spec.trajectory_path is not None:
            _, trajectory_ref = await import_workspace_file(
                context.artifacts,
                context.workspace,
                spec.trajectory_path,
                self._media_type(spec.trajectory_path),
                max_bytes=spec.max_artifact_bytes,
            )

        failures = [record for record in records if not record["success"]]
        veto_failures = [
            record for record in failures if record["severity"] in spec.veto_severities
        ]
        pass_rate = (len(records) - len(failures)) / len(records)
        outcome = {
            "schema_version": "eval.promptfoo-outcome.v1",
            "raw_results_digest": raw_ref.digest,
            "test_count": len(records),
            "pass_count": len(records) - len(failures),
            "failure_count": len(failures),
            "veto_failure_count": len(veto_failures),
            "pass_rate": pass_rate,
            "failed_test_ids": [record["test_id"] for record in failures],
            "veto_test_ids": [record["test_id"] for record in veto_failures],
        }
        outcome_ref = await context.artifacts.put_artifact(
            canonical_json_bytes(outcome),
            "application/vnd.cc-harness.promptfoo-outcome+json",
        )

        grader_results = tuple(
            GraderResult(
                grader_id=grader.grader_id,
                status=(
                    ResultStatus.PASS
                    if pass_rate >= grader.success_threshold and not veto_failures
                    else ResultStatus.FAIL
                ),
                score=pass_rate,
                message=(
                    f"Promptfoo passed {len(records) - len(failures)}/{len(records)} tests; "
                    f"veto failures={len(veto_failures)}"
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
        usage = self._usage(records)
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
            "usage": usage,
            "grader_results": grader_results,
            "metrics": (
                MetricValue(name="test-count", value=float(len(records)), unit="tests"),
                MetricValue(name="pass-rate", value=pass_rate, unit="ratio"),
                MetricValue(name="failure-count", value=float(len(failures)), unit="tests"),
                MetricValue(
                    name="veto-failure-count", value=float(len(veto_failures)), unit="tests"
                ),
            ),
            "outcome_ref": outcome_ref,
            "trajectory_ref": trajectory_ref,
            "artifacts": unique_artifacts(raw_ref, *extra_refs),
        }
        if status is ResultStatus.PASS:
            return TrialResult(**common, status=status)
        return TrialResult(
            **common,
            status=status,
            failure=FailureRecord(
                category="promptfoo-regression",
                message=(
                    f"Promptfoo evidence failed {len(failures)} tests, including "
                    f"{len(veto_failures)} veto-severity tests"
                ),
                evidence_ref=outcome_ref,
            ),
        )

    @staticmethod
    def _validate_contract(context: TrialExecutionContext, spec: PromptfooImportSpec) -> None:
        if spec.contract_id != context.request.task.task_id:
            raise EvidenceIntegrityError("Promptfoo instruction does not match the Task Contract")
        if context.request.adapter != PromptfooEvidenceAdapter.identity:
            raise EvidenceIntegrityError("Promptfoo request declares the wrong adapter identity")
        if any(grader.implementation != _IMPLEMENTATION for grader in context.request.task.graders):
            raise EvidenceIntegrityError("Promptfoo task declares an unsupported grader")

    @staticmethod
    def _parse_results(raw: bytes, minimum_test_count: int) -> list[dict[str, Any]]:
        try:
            document = json.loads(raw)
            records = document["results"]["results"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise FrameworkArtifactError("Promptfoo result JSON has an unsupported schema") from exc
        if not isinstance(records, list) or len(records) < minimum_test_count:
            raise FrameworkArtifactError(
                f"Promptfoo result count is below the contract minimum: {len(records) if isinstance(records, list) else 0}"
            )

        parsed = []
        for index, record in enumerate(records):
            if not isinstance(record, dict) or not isinstance(record.get("success"), bool):
                raise FrameworkArtifactError(f"Promptfoo result {index} lacks a boolean success")
            score = record.get("score")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise FrameworkArtifactError(f"Promptfoo result {index} lacks a numeric score")
            score = float(score)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise FrameworkArtifactError(f"Promptfoo result {index} has an invalid score")
            test_case = record.get("testCase") or {}
            metadata = test_case.get("metadata") or {}
            severity = str(metadata.get("severity", "medium")).lower()
            if severity not in {"critical", "high", "medium", "low"}:
                raise FrameworkArtifactError(f"Promptfoo result {index} has an invalid severity")
            test_id = record.get("testId") or record.get("id") or test_case.get("description")
            parsed.append(
                {
                    "raw": record,
                    "success": record["success"],
                    "score": score,
                    "severity": severity,
                    "test_id": str(test_id) if test_id is not None else f"result-{index}",
                }
            )
        return parsed

    @staticmethod
    def _usage(records: list[dict[str, Any]]) -> ResourceUsage:
        input_tokens = output_tokens = 0
        cost_usd = 0.0
        model_calls = 0
        for item in records:
            record = item["raw"]
            response = record.get("response") or {}
            token_usage = response.get("tokenUsage") or record.get("tokenUsage") or {}
            if isinstance(token_usage, dict):
                input_tokens += int(token_usage.get("prompt", token_usage.get("input", 0)) or 0)
                output_tokens += int(
                    token_usage.get("completion", token_usage.get("output", 0)) or 0
                )
            cost = response.get("cost", record.get("cost", 0.0)) or 0.0
            if isinstance(cost, (int, float)) and math.isfinite(float(cost)):
                cost_usd += max(0.0, float(cost))
            if response:
                model_calls += 1
        return ResourceUsage(
            wall_time_ms=0,
            steps=len(records),
            model_calls=model_calls,
            tool_calls=0,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=round(cost_usd * 1_000_000),
        )

    @staticmethod
    def _media_type(path: str) -> str:
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return {
            "html": "text/html",
            "json": "application/json",
            "jsonl": "application/x-ndjson",
            "md": "text/markdown",
            "txt": "text/plain",
            "yaml": "application/yaml",
            "yml": "application/yaml",
        }.get(suffix, "application/octet-stream")
