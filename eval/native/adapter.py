"""Deterministic no-shell adapter for frozen native pytest contracts."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime

from eval.core import (
    AdapterIdentity,
    ArtifactRef,
    FailureRecord,
    GraderResult,
    ResourceUsage,
    ResultStatus,
    TrialExecutionContext,
    TrialResult,
    canonical_json_bytes,
    content_fingerprint,
)
from eval.core.store import EvidenceIntegrityError

from .models import NativePytestSpec

_HOST_ENV_NAMES = {
    "COMSPEC",
    "HOME",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


class NativePytestAdapter:
    identity = AdapterIdentity(adapter_id="native-pytest", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        spec = NativePytestSpec.model_validate_json(context.instruction)
        if spec.contract_id != context.request.task.task_id:
            raise EvidenceIntegrityError("native instruction does not match the Task Contract")
        for grader in context.request.task.graders:
            if grader.implementation != "eval.native.adapter:NativePytestAdapter":
                raise EvidenceIntegrityError("native task declares an unsupported grader")

        started_at = datetime.now(UTC)
        started = time.monotonic()
        command = (sys.executable, "-m", "pytest", *spec.test_targets, "-q")
        environment = self._environment(spec)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=context.workspace,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_task = asyncio.create_task(
            self._read_limited(process.stdout, spec.output_limit_bytes, process)
        )
        stderr_task = asyncio.create_task(
            self._read_limited(process.stderr, spec.output_limit_bytes, process)
        )
        try:
            return_code = await process.wait()
            stdout, stdout_overflow = await stdout_task
            stderr, stderr_overflow = await stderr_task
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            raise

        finished_at = datetime.now(UTC)
        stdout_ref, stderr_ref = await asyncio.gather(
            context.artifacts.put_artifact(stdout, "text/plain"),
            context.artifacts.put_artifact(stderr, "text/plain"),
        )
        outcome = {
            "schema_version": "eval.native-pytest-outcome.v1",
            "argv": ["python", "-m", "pytest", *spec.test_targets, "-q"],
            "expected_exit_code": spec.expected_exit_code,
            "exit_code": return_code,
            "stdout_digest": stdout_ref.digest,
            "stderr_digest": stderr_ref.digest,
            "stdout_overflow": stdout_overflow,
            "stderr_overflow": stderr_overflow,
        }
        outcome_ref = await context.artifacts.put_artifact(
            canonical_json_bytes(outcome),
            "application/vnd.cc-harness.native-pytest-outcome+json",
        )
        usage = ResourceUsage(
            wall_time_ms=max(0, int((time.monotonic() - started) * 1000)),
            steps=1,
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_microusd=0,
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
            "finished_at": finished_at,
            "usage": usage,
        }
        artifacts = self._unique_artifacts(stdout_ref, stderr_ref)
        if stdout_overflow or stderr_overflow:
            return TrialResult(
                **common,
                status=ResultStatus.INVALID,
                artifacts=artifacts,
                invalid_reason="native pytest output exceeded the evidence capture limit",
            )

        passed = return_code == spec.expected_exit_code
        grader_status = ResultStatus.PASS if passed else ResultStatus.FAIL
        grader_results = tuple(
            GraderResult(
                grader_id=grader.grader_id,
                status=grader_status,
                score=1.0 if passed else 0.0,
                message=f"pytest exited with {return_code}; expected {spec.expected_exit_code}",
                details_ref=outcome_ref,
            )
            for grader in context.request.task.graders
        )
        if passed:
            return TrialResult(
                **common,
                status=ResultStatus.PASS,
                grader_results=grader_results,
                outcome_ref=outcome_ref,
                artifacts=artifacts,
            )
        return TrialResult(
            **common,
            status=ResultStatus.FAIL,
            grader_results=grader_results,
            outcome_ref=outcome_ref,
            artifacts=artifacts,
            failure=FailureRecord(
                category="pytest-exit",
                message=f"pytest exited with {return_code}; expected {spec.expected_exit_code}",
                evidence_ref=outcome_ref,
            ),
        )

    @staticmethod
    def _environment(spec: NativePytestSpec) -> dict[str, str]:
        allowed = _HOST_ENV_NAMES | set(spec.environment)
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment.update(
            {
                "CI": "1",
                "NO_COLOR": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return environment

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader,
        limit: int,
        process: asyncio.subprocess.Process,
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        overflow = False
        while chunk := await stream.read(64 * 1024):
            previous_total = total
            total += len(chunk)
            if previous_total < limit:
                retained = chunk[: limit - previous_total]
                chunks.append(retained)
            if total > limit:
                overflow = True
            if overflow and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
        return b"".join(chunks), overflow

    @staticmethod
    def _unique_artifacts(*references: ArtifactRef) -> tuple[ArtifactRef, ...]:
        return tuple({reference.digest: reference for reference in references}.values())
