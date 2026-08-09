"""Live harness launcher and deterministic canary grader."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.core import (
    AdapterIdentity,
    ArtifactRef,
    BudgetEnforcement,
    FailureRecord,
    GraderResult,
    ResourceUsage,
    ResultStatus,
    TrialExecutionContext,
    TrialResult,
    canonical_json_bytes,
    content_fingerprint,
)
from eval.core.framework_artifacts import unique_artifacts
from eval.core.store import EvidenceIntegrityError
from eval.launch import (
    HarnessKind,
    LaunchProfile,
    LaunchRequest,
    build_invocation,
    run_invocation,
)

from .models import CanaryInstruction

_IMPLEMENTATION = "eval.canary.adapter:HarnessCanaryAdapter"
_ALLOCATED_SANDBOX_PORTS: set[int] = set()
_TRANSIENT_PROVIDER_PATTERNS = (
    re.compile(
        r"\b(?:http(?:/\d(?:\.\d)?)?(?:\s+status)?|status(?:\s+code)?|"
        r"api(?:\s+error)?|error(?:\s+code)?|response(?:\s+code)?)"
        r"\s*[:=]?\s*(?:429|500|502|503|504)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b429\s+(?:too many requests|rate limit(?:ed|ing)?)\b", re.IGNORECASE),
    re.compile(r"\b500\s+internal(?:\s+server)?\s+error\b", re.IGNORECASE),
    re.compile(r"\b502\s+bad\s+gateway\b", re.IGNORECASE),
    re.compile(r"\b503\s+service\s+unavailable\b", re.IGNORECASE),
    re.compile(r"\b504\s+gateway\s+timeout\b", re.IGNORECASE),
    re.compile(
        r"\b(?:connection reset(?: by peer)?|rate limit(?:ed|ing| exceeded)?|"
        r"retry budget(?: exhausted)?|service unavailable|service_unavailable|"
        r"temporarily unavailable|timeout connecting)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:error|message)\W{1,20}overloaded(?:_error)?\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class _Grade:
    exit_code: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool = False


class HarnessCanaryAdapter:
    """Run one harness profile and normalize all evidence into a TrialResult."""

    def __init__(
        self,
        profile: LaunchProfile,
        *,
        source_environment: Mapping[str, str] | None = None,
        environment_files: tuple[Path, ...] = (),
    ) -> None:
        if profile.harness not in {HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE}:
            raise ValueError("canary parity supports only cc-harness and Claude Code")
        self.profile = profile
        self.source_environment = dict(source_environment or os.environ)
        self.environment_files = tuple(environment_files)
        self.identity = AdapterIdentity(
            adapter_id=f"canary-{profile.harness.value}",
            adapter_version="1.0.0",
        )

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        instruction = CanaryInstruction.model_validate_json(context.instruction)
        self._validate_contract(context, instruction)
        started_at = datetime.now(UTC)
        started = time.monotonic()
        launch_request = LaunchRequest(
            prompt=instruction.prompt, budget=context.request.task.budget
        )
        launch_environment = self.source_environment
        sandbox_config_path: Path | None = None
        if self.profile.harness is HarnessKind.CC_HARNESS:
            launch_environment, sandbox_config_path = _owned_sandbox_environment(
                self.source_environment,
                context.workspace,
            )
        invocation = build_invocation(
            self.profile,
            launch_request,
            context.workspace,
            source_environment=launch_environment,
            environment_files=self.environment_files,
        )
        budget = context.request.task.budget
        launch_timeout = max(
            1,
            budget.execution_timeout_seconds - instruction.grader_timeout_seconds - 1,
        )
        try:
            completed = await run_invocation(
                self.profile,
                invocation,
                timeout_seconds=launch_timeout,
            )
        finally:
            if sandbox_config_path is not None:
                sandbox_config_path.unlink(missing_ok=True)
                with suppress(OSError):
                    sandbox_config_path.parent.rmdir()
        launch_ref, stdout_ref, stderr_ref = await asyncio.gather(
            context.artifacts.put_artifact(
                canonical_json_bytes(completed.evidence),
                "application/vnd.cc-harness.launch-evidence+json",
            ),
            context.artifacts.put_artifact(completed.stdout, "application/x-ndjson"),
            context.artifacts.put_artifact(completed.stderr, "text/plain"),
        )
        base_artifacts = unique_artifacts(launch_ref, stdout_ref, stderr_ref)
        usage = ResourceUsage(
            wall_time_ms=completed.evidence.wall_time_ms,
            steps=max(1, completed.evidence.model_calls),
            model_calls=completed.evidence.model_calls,
            tool_calls=completed.evidence.tool_calls,
            input_tokens=completed.evidence.input_tokens,
            output_tokens=completed.evidence.output_tokens,
            cost_microusd=completed.evidence.cost_microusd,
        )
        if not completed.evidence.valid_for_parity:
            reason = _launch_failure_reason(
                completed.evidence,
                completed.stdout,
                completed.stderr,
                emergency_watchdog=budget.enforcement is BudgetEnforcement.OBSERVE,
            )
            outcome_ref = await self._outcome(
                context,
                instruction,
                launch_ref,
                grade=None,
                protected_ok=None,
                outcome_refs=(),
                missing_paths=(),
            )
            return self._invalid_result(
                context,
                started_at,
                started,
                usage,
                reason,
                outcome_ref,
                stdout_ref,
                base_artifacts,
            )

        protected_ok = self._protected_files_unchanged(context.workspace, instruction)
        if protected_ok:
            grade = await _run_pytest(
                context.workspace,
                instruction.test_targets,
                timeout_seconds=instruction.grader_timeout_seconds,
                output_limit_bytes=instruction.output_limit_bytes,
            )
        else:
            grade = _Grade(
                exit_code=-2,
                stdout=b"",
                stderr=b"protected test files changed; grader was not executed",
                stdout_overflow=False,
                stderr_overflow=False,
                timed_out=False,
            )
        grade_stdout_ref, grade_stderr_ref = await asyncio.gather(
            context.artifacts.put_artifact(grade.stdout, "text/plain"),
            context.artifacts.put_artifact(grade.stderr, "text/plain"),
        )
        outcome_refs, missing_paths = await self._capture_outcomes(
            context,
            instruction.outcome_paths,
        )
        outcome_ref = await self._outcome(
            context,
            instruction,
            launch_ref,
            grade=grade,
            protected_ok=protected_ok,
            outcome_refs=outcome_refs,
            missing_paths=missing_paths,
        )
        artifacts = unique_artifacts(
            *base_artifacts,
            grade_stdout_ref,
            grade_stderr_ref,
            *outcome_refs,
        )
        usage = usage.model_copy(
            update={"wall_time_ms": max(0, round((time.monotonic() - started) * 1000))}
        )
        if grade.stdout_overflow or grade.stderr_overflow:
            return self._invalid_result(
                context,
                started_at,
                started,
                usage,
                "grader output exceeded the evidence capture limit",
                outcome_ref,
                stdout_ref,
                artifacts,
            )
        if grade.timed_out:
            return self._invalid_result(
                context,
                started_at,
                started,
                usage,
                "deterministic grader exceeded its timeout",
                outcome_ref,
                stdout_ref,
                artifacts,
            )

        passed = grade.exit_code == 0 and protected_ok and not missing_paths
        status = ResultStatus.PASS if passed else ResultStatus.FAIL
        details = []
        if grade.exit_code != 0:
            details.append(f"pytest exited with {grade.exit_code}")
        if not protected_ok:
            details.append("protected test files changed")
        if missing_paths:
            details.append(f"missing outcome files: {', '.join(missing_paths)}")
        message = "canary passed" if passed else "; ".join(details)
        common = self._result_fields(
            context,
            started_at,
            usage,
            status,
            outcome_ref,
            stdout_ref,
            artifacts,
        )
        grader_results = tuple(
            GraderResult(
                grader_id=grader.grader_id,
                status=status,
                score=1.0 if passed else 0.0,
                message=message,
                details_ref=outcome_ref,
            )
            for grader in context.request.task.graders
        )
        if passed:
            return TrialResult(**common, grader_results=grader_results)
        return TrialResult(
            **common,
            grader_results=grader_results,
            failure=FailureRecord(
                category="canary-grader-failure",
                message=message,
                evidence_ref=outcome_ref,
            ),
        )

    def _validate_contract(
        self,
        context: TrialExecutionContext,
        instruction: CanaryInstruction,
    ) -> None:
        if instruction.contract_id != context.request.task.task_id:
            raise EvidenceIntegrityError("canary instruction does not match the Task Contract")
        if instruction.requested_model != self.profile.requested_model:
            raise EvidenceIntegrityError("canary instruction and profile models differ")
        if context.request.adapter != self.identity:
            raise EvidenceIntegrityError("canary request declares the wrong adapter identity")
        if any(grader.implementation != _IMPLEMENTATION for grader in context.request.task.graders):
            raise EvidenceIntegrityError("canary task declares an unsupported grader")

    @staticmethod
    def _protected_files_unchanged(workspace: Path, instruction: CanaryInstruction) -> bool:
        for protected in instruction.protected_files:
            path = _workspace_file(workspace, protected.path)
            if not path.is_file():
                return False
            actual = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            if actual != protected.digest:
                return False
        return True

    @staticmethod
    async def _capture_outcomes(
        context: TrialExecutionContext,
        paths: tuple[str, ...],
    ) -> tuple[tuple[ArtifactRef, ...], tuple[str, ...]]:
        references: list[ArtifactRef] = []
        missing: list[str] = []
        for relative in paths:
            path = _workspace_file(context.workspace, relative)
            if not path.is_file():
                missing.append(relative)
                continue
            references.append(await context.artifacts.put_artifact(path.read_bytes(), "text/plain"))
        return tuple(references), tuple(missing)

    @staticmethod
    async def _outcome(
        context: TrialExecutionContext,
        instruction: CanaryInstruction,
        launch_ref: ArtifactRef,
        *,
        grade: _Grade | None,
        protected_ok: bool | None,
        outcome_refs: tuple[ArtifactRef, ...],
        missing_paths: tuple[str, ...],
    ) -> ArtifactRef:
        document = {
            "schema_version": "eval.canary-outcome.v1",
            "task_id": instruction.contract_id,
            "launch_evidence_digest": launch_ref.digest,
            "grader_exit_code": None if grade is None else grade.exit_code,
            "grader_timed_out": None if grade is None else grade.timed_out,
            "protected_files_unchanged": protected_ok,
            "outcome_digests": [item.digest for item in outcome_refs],
            "missing_outcome_paths": list(missing_paths),
        }
        return await context.artifacts.put_artifact(
            canonical_json_bytes(document),
            "application/vnd.cc-harness.canary-outcome+json",
        )

    def _invalid_result(
        self,
        context: TrialExecutionContext,
        started_at: datetime,
        started: float,
        usage: ResourceUsage,
        reason: str,
        outcome_ref: ArtifactRef,
        trajectory_ref: ArtifactRef,
        artifacts: tuple[ArtifactRef, ...],
    ) -> TrialResult:
        usage = usage.model_copy(
            update={"wall_time_ms": max(0, round((time.monotonic() - started) * 1000))}
        )
        return TrialResult(
            **self._result_fields(
                context,
                started_at,
                usage,
                ResultStatus.INVALID,
                outcome_ref,
                trajectory_ref,
                artifacts,
            ),
            invalid_reason=reason,
        )

    def _result_fields(
        self,
        context: TrialExecutionContext,
        started_at: datetime,
        usage: ResourceUsage,
        status: ResultStatus,
        outcome_ref: ArtifactRef,
        trajectory_ref: ArtifactRef,
        artifacts: tuple[ArtifactRef, ...],
    ) -> dict:
        return {
            "trial_id": context.request.trial_id,
            "run_id": context.request.run_id,
            "run_manifest_digest": context.request.run_manifest_digest,
            "task_id": context.request.task.task_id,
            "task_contract_digest": content_fingerprint(context.request.task),
            "attempt": context.attempt,
            "adapter": self.identity,
            "status": status,
            "started_at": started_at,
            "finished_at": datetime.now(UTC),
            "usage": usage,
            "outcome_ref": outcome_ref,
            "trajectory_ref": trajectory_ref,
            "artifacts": artifacts,
        }


def _owned_sandbox_environment(
    source_environment: Mapping[str, str],
    workspace: Path,
) -> tuple[dict[str, str], Path]:
    """Give each evaluated cc-harness process an owned, attestable server."""
    config_dir = workspace.parent / ".sandbox-configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{workspace.name}.toml"
    environment = dict(source_environment)
    environment["CC_HARNESS_SANDBOX_SERVER_PORT"] = str(_available_loopback_port())
    environment["CC_HARNESS_SANDBOX_SERVER_CONFIG_PATH"] = str(config_path)
    return environment, config_path


def _available_loopback_port() -> int:
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = int(candidate.getsockname()[1])
        if port not in _ALLOCATED_SANDBOX_PORTS:
            _ALLOCATED_SANDBOX_PORTS.add(port)
            return port
    raise RuntimeError("could not allocate a unique OpenSandbox loopback port")


async def _run_pytest(
    workspace: Path,
    targets: tuple[str, ...],
    *,
    timeout_seconds: int,
    output_limit_bytes: int,
) -> _Grade:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
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
    }
    environment.update(
        {"CI": "1", "NO_COLOR": "1", "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        *targets,
        "-q",
        cwd=workspace,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, output_limit_bytes))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, output_limit_bytes))
    try:
        timed_out = False
        async with asyncio.timeout(timeout_seconds):
            exit_code = await process.wait()
    except TimeoutError:
        timed_out = True
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        exit_code = -1
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
    stdout, stdout_overflow = await stdout_task
    stderr, stderr_overflow = await stderr_task
    return _Grade(exit_code, stdout, stderr, stdout_overflow, stderr_overflow, timed_out)


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    overflow = False
    while chunk := await reader.read(64 * 1024):
        room = max(0, limit - retained)
        if room:
            chunks.append(chunk[:room])
            retained += min(room, len(chunk))
        if len(chunk) > room:
            overflow = True
    return b"".join(chunks), overflow


def _workspace_file(workspace: Path, relative: str) -> Path:
    root = workspace.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EvidenceIntegrityError("canary path escapes the workspace") from exc
    return path


def _launch_failure_reason(
    evidence,
    stdout: bytes,
    stderr: bytes,
    *,
    emergency_watchdog: bool = False,
) -> str:
    detail = " ".join(
        part
        for part in (
            evidence.parse_error,
            stdout.decode("utf-8", errors="replace")[-2_000:],
            stderr.decode("utf-8", errors="replace")[-2_000:],
        )
        if part
    ).lower()
    provider_marker = _transient_provider_marker(detail)
    if provider_marker is not None:
        prefix = "transient provider failure"
    elif evidence.timed_out and emergency_watchdog:
        prefix = "emergency watchdog timeout"
    elif evidence.timed_out:
        prefix = "launch wall-time timeout"
    else:
        prefix = "launch evidence invalid"
    flags = []
    if evidence.timed_out:
        flags.append("timed out")
    if evidence.stdout_truncated or evidence.stderr_truncated:
        flags.append("output truncated")
    if evidence.exit_code != 0:
        flags.append(f"exit code {evidence.exit_code}")
    if evidence.parse_error:
        flags.append(evidence.parse_error[:500])
    if evidence.resolved_model != evidence.requested_model:
        flags.append("resolved model mismatch")
    if provider_marker is not None:
        flags.append(f"provider marker {provider_marker!r}")
    return f"{prefix}: {', '.join(flags) or 'structured evidence rejected'}"


def _transient_provider_marker(detail: str) -> str | None:
    for pattern in _TRANSIENT_PROVIDER_PATTERNS:
        if match := pattern.search(detail):
            return match.group(0)
    return None


def is_transient_provider_result(result: TrialResult) -> bool:
    return (
        result.status is ResultStatus.INVALID
        and result.invalid_reason is not None
        and result.invalid_reason.startswith("transient provider failure:")
    )
