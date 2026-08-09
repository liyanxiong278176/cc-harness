"""Strict importer for Harbor and Terminal-Bench TrialResult JSON."""

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

from .models import HarborImportSpec

_IMPLEMENTATION = "eval.harbor.adapter:HarborEvidenceAdapter"


class HarborEvidenceAdapter:
    identity = AdapterIdentity(adapter_id="harbor-import", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        spec = HarborImportSpec.model_validate_json(context.instruction)
        self._validate_contract(context, spec)
        raw, raw_ref = await import_workspace_file(
            context.artifacts,
            context.workspace,
            spec.result_path,
            "application/vnd.harbor.trial-result+json",
            max_bytes=spec.max_artifact_bytes,
        )
        record = self._parse_result(raw, spec)
        artifact_refs = [raw_ref]
        for path in spec.artifact_paths:
            _, reference = await import_workspace_file(
                context.artifacts,
                context.workspace,
                path,
                "application/octet-stream",
                max_bytes=spec.max_artifact_bytes,
            )
            artifact_refs.append(reference)
        trajectory_ref = None
        if spec.trajectory_path:
            _, trajectory_ref = await import_workspace_file(
                context.artifacts,
                context.workspace,
                spec.trajectory_path,
                "application/x-ndjson",
                max_bytes=spec.max_artifact_bytes,
            )

        reward = record["reward"]
        passed = reward >= spec.success_threshold and record["exception"] is None
        outcome_ref = await context.artifacts.put_artifact(
            canonical_json_bytes(
                {
                    "schema_version": "eval.harbor-outcome.v1",
                    "raw_result_digest": raw_ref.digest,
                    "task_name": record["task_name"],
                    "trial_name": record["trial_name"],
                    "resolved_model": record["model"],
                    "reward_key": spec.reward_key,
                    "reward": reward,
                    "passed": passed,
                }
            ),
            "application/vnd.cc-harness.harbor-outcome+json",
        )
        status = ResultStatus.PASS if passed else ResultStatus.FAIL
        grader_results = tuple(
            GraderResult(
                grader_id=grader.grader_id,
                status=status,
                score=reward,
                message=f"Harbor reward {spec.reward_key}={reward:.4f}",
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
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "usage": record["usage"],
            "grader_results": grader_results,
            "metrics": (MetricValue(name="reward", value=reward, unit="ratio"),),
            "outcome_ref": outcome_ref,
            "trajectory_ref": trajectory_ref,
            "artifacts": unique_artifacts(*artifact_refs),
        }
        if passed:
            return TrialResult(**common)
        return TrialResult(
            **common,
            failure=FailureRecord(
                category="harbor-task-failure",
                message=record["exception"] or f"reward {reward:.4f} below threshold",
                evidence_ref=outcome_ref,
            ),
        )

    @staticmethod
    def _validate_contract(context: TrialExecutionContext, spec: HarborImportSpec) -> None:
        if spec.contract_id != context.request.task.task_id:
            raise EvidenceIntegrityError("Harbor instruction does not match the Task Contract")
        if context.request.adapter != HarborEvidenceAdapter.identity:
            raise EvidenceIntegrityError("Harbor request declares the wrong adapter identity")
        if any(grader.implementation != _IMPLEMENTATION for grader in context.request.task.graders):
            raise EvidenceIntegrityError("Harbor task declares an unsupported grader")

    @staticmethod
    def _parse_result(raw: bytes, spec: HarborImportSpec) -> dict[str, Any]:
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameworkArtifactError("Harbor result is not valid JSON") from exc
        if not isinstance(document, dict):
            raise FrameworkArtifactError("Harbor result must be a JSON object")
        if document.get("task_name") != spec.expected_task_name:
            raise FrameworkArtifactError("Harbor task_name does not match the contract")
        if document.get("task_checksum") != spec.expected_task_checksum:
            raise FrameworkArtifactError("Harbor task_checksum does not match the contract")
        trial_name = document.get("trial_name")
        if not isinstance(trial_name, str) or not trial_name:
            raise FrameworkArtifactError("Harbor result lacks trial_name")
        if spec.expected_trial_name is not None and trial_name != spec.expected_trial_name:
            raise FrameworkArtifactError("Harbor trial_name does not match the contract")
        model = ((document.get("agent_info") or {}).get("model_info") or {}).get("name")
        if model != "deepseek-v4-flash":
            raise FrameworkArtifactError(f"Harbor resolved model mismatch: {model!r}")
        rewards = (document.get("verifier_result") or {}).get("rewards")
        if not isinstance(rewards, dict) or spec.reward_key not in rewards:
            raise FrameworkArtifactError(f"Harbor result lacks verifier reward {spec.reward_key!r}")
        reward = rewards[spec.reward_key]
        if (
            isinstance(reward, bool)
            or not isinstance(reward, (int, float))
            or not math.isfinite(float(reward))
            or not 0 <= float(reward) <= 1
        ):
            raise FrameworkArtifactError("Harbor reward must be finite and between zero and one")
        started = _timestamp(document.get("started_at"), "started_at")
        finished = _timestamp(document.get("finished_at"), "finished_at")
        if finished < started:
            raise FrameworkArtifactError("Harbor finished_at precedes started_at")
        contexts = (
            [document.get("agent_result")]
            if document.get("agent_result")
            else [
                step.get("agent_result")
                for step in document.get("step_results") or []
                if isinstance(step, dict) and step.get("agent_result")
            ]
        )
        usage = _usage(contexts, started, finished)
        exception = document.get("exception_info")
        if exception is not None and not isinstance(exception, dict):
            raise FrameworkArtifactError("Harbor exception_info has an invalid schema")
        message = None
        if exception is not None:
            message = str(
                exception.get("exception_message")
                or exception.get("exception_type")
                or "Harbor trial failed"
            )
        return {
            "task_name": document["task_name"],
            "trial_name": trial_name,
            "model": model,
            "reward": float(reward),
            "started_at": started,
            "finished_at": finished,
            "usage": usage,
            "exception": message,
        }


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise FrameworkArtifactError(f"Harbor result lacks {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FrameworkArtifactError(f"Harbor {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise FrameworkArtifactError(f"Harbor {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _usage(contexts: list[dict[str, Any]], started: datetime, finished: datetime) -> ResourceUsage:
    def total(name: str) -> int:
        values = [item.get(name) for item in contexts if isinstance(item, dict)]
        if any(
            isinstance(value, bool)
            or (value is not None and (not isinstance(value, int) or value < 0))
            for value in values
        ):
            raise FrameworkArtifactError(f"Harbor agent result has invalid {name}")
        return sum(value or 0 for value in values)

    costs = [item.get("cost_usd") for item in contexts if isinstance(item, dict)]
    if any(
        isinstance(value, bool)
        or (
            value is not None
            and (
                not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0
            )
        )
        for value in costs
    ):
        raise FrameworkArtifactError("Harbor agent result has invalid cost_usd")
    return ResourceUsage(
        wall_time_ms=round((finished - started).total_seconds() * 1000),
        steps=max(1, len(contexts)),
        model_calls=max(1, len(contexts)),
        tool_calls=0,
        input_tokens=total("n_input_tokens"),
        output_tokens=total("n_output_tokens"),
        cost_microusd=round(sum(float(value or 0) for value in costs) * 1_000_000),
    )
