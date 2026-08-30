"""Harbor-backed single-system adapters for coding and terminal benchmarks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from cc_harness.executor import _process_activity_snapshot
from eval.harbor.paired import HARBOR_VERSION, build_harbor_command
from eval.launch import HarnessKind
from eval.launch.runner import _terminate_process_tree

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from ..infrastructure import (
    transient_infrastructure_text,
    verifier_bootstrap_failure_text,
)
from ..terminal_bootstrap import (
    UV_BOOTSTRAP_SHA256,
    ensure_uv_bootstrap,
    uv_bootstrap_cache_path,
    uv_bootstrap_identity,
)
from ..terminal_host import inspect_terminal_host
from ..tiktoken_bootstrap import (
    ensure_tiktoken_bootstrap,
    tiktoken_bootstrap_cache_path,
    tiktoken_bootstrap_identity,
)
from ..verifier_runtime import agent_runtime_overlay, ensure_verifier_runtime

SWEBENCH_DATASET = "swe-bench/swe-bench-verified@sha256:b934b0cc3dc800fe945eaf9f1623329db97ee5e27c24c5563b3a16c6e2854c17"
TERMINAL_BENCH_20_DATASET = "terminal-bench@2.0"
TERMINAL_BENCH_21_DATASET = (
    "terminal-bench/terminal-bench-2-1@"
    "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
)
TERMINAL_BENCH_DATASET = TERMINAL_BENCH_21_DATASET
_TERMINAL_HEARTBEAT_SECONDS = 15
_TERMINAL_MIN_FREE_BYTES = 80 * 1024**3
_TERMINAL_MAX_ITERATIONS = 80
_DOCKER_HEALTHCHECK_ATTEMPTS = 3
_DOCKER_HEALTHCHECK_BACKOFFS = (1.0, 2.0)
_DOCKER_HEALTHCHECK_TIMEOUT_SECONDS = 10
_HARBOR_HOST_IMPORT_TIMEOUT_SECONDS = 60
_OFFICIAL_TERMINAL_FORBIDDEN_OPTIONS = (
    "--timeout-multiplier",
    "--agent-timeout-multiplier",
    "--agent-setup-timeout-multiplier",
    "--environment-build-timeout-multiplier",
    "--verifier-timeout-multiplier",
    "--allow-agent-host",
)


def _assert_official_terminal_launch(command: Sequence[str], overlay: Path) -> None:
    """Fail closed if the reportable path mutates task/verifier semantics."""

    if TERMINAL_BENCH_21_DATASET not in command:
        raise RuntimeError("formal Terminal-Bench launch is not pinned to the official dataset")
    forbidden = sorted(option for option in _OFFICIAL_TERMINAL_FORBIDDEN_OPTIONS if option in command)
    if forbidden:
        raise RuntimeError(f"formal Terminal-Bench launch contains protocol overrides: {forbidden}")
    try:
        payload = json.loads(overlay.read_text(encoding="utf-8"))
        service = payload["services"]["main"]
        environment = service.get("environment") or {}
        volumes = [str(value) for value in service.get("volumes") or []]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("formal custom-agent overlay is malformed") from exc
    if environment != {"CC_HARNESS_TERMINAL_AGENT_RUNTIME": "1"}:
        raise RuntimeError("formal custom-agent overlay changes the task environment")
    forbidden_text = "\n".join(volumes).casefold()
    for marker in (
        "verifier-offline-bin",
        "/tests",
        ":/usr/",
        ":/bin/",
        "/root/.local/bin/env",
        "/root/.local/bin/uvx",
        "/root/.local/bin/uv",
    ):
        if marker in forbidden_text:
            raise RuntimeError(f"formal custom-agent overlay modifies verifier path: {marker}")
    allowed_targets = {
        "/opt/cc-harness/agent-runtime/python:ro",
        "/opt/cc-harness/agent-runtime/lib:ro",
        "/opt/cc-harness/agent-site:ro",
        "/root/.local/bin/cc-harness:ro",
    }
    targets = {
        ":".join(value.rsplit(":", 2)[-2:])
        for value in volumes
        if len(value.rsplit(":", 2)) >= 3
    }
    if targets != allowed_targets:
        raise RuntimeError(f"formal custom-agent overlay has unexpected mounts: {targets}")


def _terminal_agent_timeout(task_id: str, source_digest: str | None) -> float | None:
    """Read the public official agent timeout without loading task instructions."""

    name = task_id.rsplit("/", 1)[-1]
    digest = str(source_digest or "").removeprefix("sha256:")
    package_root = Path.home() / ".cache" / "harbor" / "tasks" / "packages" / "terminal-bench" / name
    candidates = [package_root / digest / "task.toml"] if digest else []
    candidates.extend(sorted(package_root.glob("*/task.toml")))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            timeout = float((payload.get("agent") or {}).get("timeout_sec") or 0)
        except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
            continue
        if timeout > 0:
            return timeout
    return None


def _terminal_agent_progress_snapshot(jobs: Path) -> dict[str, int | str]:
    """Return cheap liveness evidence from the in-container agent loop.

    Harbor's process tree can remain almost idle while the agent is waiting on
    a model response or running a command in a child container.  The agent
    writes its structured progress file under ``<job>/agent``; observing its
    size/mtime makes that work visible to the outer supervisor without reading
    the whole transcript on every heartbeat.
    """

    candidates: list[Path] = []
    # A Harbor job normally has exactly jobs/<date>/<task>/agent/<file>.  Keep
    # the fallback one level shallower for older Harbor layouts, and cap the
    # scan so a task workspace containing many files cannot make heartbeats
    # expensive.
    for pattern in (
        "*/*/*/agent/cc-harness-progress.jsonl",
        "*/*/agent/cc-harness-progress.jsonl",
        "agent/cc-harness-progress.jsonl",
    ):
        try:
            candidates.extend(path for path in jobs.glob(pattern) if path.is_file())
        except OSError:
            continue
        if len(candidates) >= 8:
            break
    if not candidates:
        return {
            "agent_progress_files": 0,
            "agent_progress_bytes": 0,
            "agent_progress_mtime_ns": 0,
            "agent_last_event": "none",
        }
    try:
        latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        stat = latest.stat()
    except OSError:
        return {
            "agent_progress_files": len(candidates),
            "agent_progress_bytes": 0,
            "agent_progress_mtime_ns": 0,
            "agent_last_event": "unavailable",
        }
    last_event = "unknown"
    try:
        with latest.open("rb") as handle:
            handle.seek(max(0, stat.st_size - 4096))
            tail = handle.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                last_event = line.strip()[:120]
            else:
                if isinstance(record, Mapping):
                    event = str(record.get("event") or "unknown")
                    command = str(record.get("command") or record.get("command_digest") or "")
                    last_event = f"{event}:{command[:48]}" if command else event
                else:
                    last_event = str(record)[:120]
            break
    except OSError:
        last_event = "unavailable"
    return {
        "agent_progress_files": len(candidates),
        "agent_progress_bytes": int(stat.st_size),
        "agent_progress_mtime_ns": int(stat.st_mtime_ns),
        "agent_last_event": last_event.replace("\r", " ").replace("\n", " ")[:120],
    }

_SWEBENCH_PORTFOLIO = (
    "swe-bench/astropy__astropy-14995", "swe-bench/astropy__astropy-7166",
    "swe-bench/astropy__astropy-13033", "swe-bench/astropy__astropy-13398",
    "swe-bench/django__django-9296", "swe-bench/django__django-17087",
    "swe-bench/django__django-12193", "swe-bench/django__django-16662",
    "swe-bench/django__django-11555", "swe-bench/django__django-13820",
    "swe-bench/django__django-15277", "swe-bench/django__django-16899",
    "swe-bench/matplotlib__matplotlib-20488", "swe-bench/matplotlib__matplotlib-23412",
    "swe-bench/matplotlib__matplotlib-23476", "swe-bench/matplotlib__matplotlib-23314",
    "swe-bench/matplotlib__matplotlib-21568", "swe-bench/mwaskom__seaborn-3187",
    "swe-bench/mwaskom__seaborn-3069", "swe-bench/pallets__flask-5014",
    "swe-bench/psf__requests-5414", "swe-bench/psf__requests-1724",
    "swe-bench/psf__requests-1921", "swe-bench/pydata__xarray-4966",
    "swe-bench/pydata__xarray-7233", "swe-bench/pydata__xarray-4075",
    "swe-bench/pydata__xarray-6461", "swe-bench/pylint-dev__pylint-4551",
    "swe-bench/pylint-dev__pylint-7080", "swe-bench/pylint-dev__pylint-6528",
    "swe-bench/pytest-dev__pytest-7432", "swe-bench/pytest-dev__pytest-10356",
    "swe-bench/pytest-dev__pytest-10051", "swe-bench/pytest-dev__pytest-8399",
    "swe-bench/scikit-learn__scikit-learn-12585",
    "swe-bench/scikit-learn__scikit-learn-10908",
    "swe-bench/scikit-learn__scikit-learn-14629",
    "swe-bench/scikit-learn__scikit-learn-12973",
    "swe-bench/sphinx-doc__sphinx-10449", "swe-bench/sphinx-doc__sphinx-7440",
    "swe-bench/sphinx-doc__sphinx-8056", "swe-bench/sphinx-doc__sphinx-9591",
    "swe-bench/sphinx-doc__sphinx-9461", "swe-bench/sympy__sympy-19637",
    "swe-bench/sympy__sympy-13757", "swe-bench/sympy__sympy-12419",
    "swe-bench/sympy__sympy-13615", "swe-bench/sympy__sympy-22914",
    "swe-bench/sympy__sympy-22714", "swe-bench/sympy__sympy-20916",
)

_TERMINAL_PORTFOLIO = (
    "compile-compcert", "build-pov-ray", "build-cython-ext", "modernize-scientific-stack",
    "cobol-modernization", "configure-git-webserver", "nginx-request-logging",
    "qemu-alpine-ssh", "mailman", "fix-git", "pytorch-model-recovery",
    "torch-tensor-parallelism", "train-fasttext", "reshard-c4-data",
    "llm-inference-batching-scheduler", "largest-eigenval", "raman-fitting",
    "protein-assembly", "mcmc-sampling-stan", "portfolio-optimization",
    "password-recovery", "crack-7z-hash", "db-wal-recovery", "git-leak-recovery",
    "fix-code-vulnerability", "video-processing", "extract-moves-from-video",
    "code-from-image", "sam-cell-seg", "extract-elf",
)


class _HarborAdapter:
    capability_profile = "clean-coding"
    adaptations: tuple[str, ...] = ()
    dataset: str

    def __init__(self, wheel_path: Path | None = None) -> None:
        self.wheel_path = wheel_path

    def check(self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]) -> CheckResult:
        del profile
        wheel = self._wheel(project_root)
        requirements = {
            "project_env": (project_root / ".env").is_file(),
            "uvx": shutil.which("uvx") is not None,
            "docker": shutil.which("docker") is not None,
            "wheel": wheel.is_file(),
        }
        warnings = tuple(name + " is unavailable" for name, ready in requirements.items() if not ready)
        return CheckResult(
            ready=all(requirements.values()) and bool(tasks),
            details={
                "requirements": requirements,
                "wheel": str(wheel),
                "dataset": self.dataset,
                "harbor_version": HARBOR_VERSION,
                "task_count": len(tasks),
                "single_pass": True,
            },
            warnings=warnings,
        )

    async def execute(self, context: TrialContext) -> TrialOutcome:
        evidence_jobs = context.attempt_root / "jobs"
        jobs = evidence_jobs
        if self.slug.startswith("terminal-bench"):
            runtime_root = os.environ.get("CC_HARNESS_TERMINAL_RUNTIME_ROOT")
            if not runtime_root:
                raise RuntimeError(
                    "Terminal-Bench requires CC_HARNESS_TERMINAL_RUNTIME_ROOT"
                )
            run_identity = hashlib.sha256(
                str(context.output_root.resolve()).encode("utf-8")
            ).hexdigest()[:16]
            task_identity = hashlib.sha256(
                context.task.task_id.encode("utf-8")
            ).hexdigest()[:12]
            jobs = (
                Path(runtime_root)
                / run_identity
                / f"{context.task_index:04d}-{task_identity}"
                / f"attempt-{context.attempt}"
                / "jobs"
            )
        # A cancelled Terminal-Bench task reopens the same logical attempt;
        # retain the Harbor jobs directory and any workspace/checkpoint files.
        jobs.mkdir(parents=True, exist_ok=True)
        runtime_context_path = context.attempt_root / "runtime-context.json"
        previous_context: dict[str, Any] = {}
        if runtime_context_path.is_file():
            try:
                previous_context = json.loads(runtime_context_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                previous_context = {}
        runtime_context_path.write_text(
            json.dumps(
                {
                    "task_id": context.task.task_id,
                    "attempt": context.attempt,
                    "task_index": context.task_index,
                    "task_total": context.task_total,
                    "resume_count": int(previous_context.get("resume_count") or 0) + 1,
                    "checkpoint_policy": "reuse-same-attempt",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        docker_before = _docker_snapshot()
        (context.attempt_root / "docker-before.json").write_text(
            json.dumps(docker_before, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        docker_health = _docker_healthcheck()
        (context.attempt_root / "docker-health.json").write_text(
            json.dumps(docker_health, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if not docker_health["ready"]:
            diagnostic = str(docker_health.get("last_error") or "Docker daemon unavailable")
            return TrialOutcome(
                # No Harbor/model work has started at this point. Keep this
                # as infrastructure evidence so the runner can retry the
                # same checkpoint without publishing a false task failure.
                status=TrialStatus.INVALID,
                invalid_reason=(
                    "Docker daemon health check failed after "
                    f"{docker_health['attempts']} attempts: {diagnostic}"
                ),
                protocol={
                    "exception_is_infrastructure": True,
                    "transient_infrastructure": True,
                    "docker_healthcheck": docker_health,
                    "official_error_counted_as_zero": False,
                },
            )
        configured = {
            key: str(value)
            for key, value in dotenv_values(context.project_root / ".env").items()
            if value is not None
        }
        env_file = context.project_root / ".env"
        launch_values = configured
        if self.slug.startswith("terminal-bench"):
            real_key = configured.get("OPENAI_API_KEY")
            real_base = configured.get("OPENAI_BASE_URL")
            if not real_key or not real_base:
                raise ValueError("Terminal-Bench requires OPENAI_API_KEY and OPENAI_BASE_URL")
        terminal_overlay = None
        terminal_timeout = None
        if self.slug.startswith("terminal-bench"):
            terminal_overlay = agent_runtime_overlay(context.project_root, context.output_root)
            terminal_timeout = _terminal_agent_timeout(
                str(context.task.task_id), context.task.payload.get("source_digest")
            )
        terminal_agent_env = {
            "CC_HARNESS_TERMINAL_BENCH": "1",
            "CC_HARNESS_TERMINAL_MAX_ITERATIONS": str(_TERMINAL_MAX_ITERATIONS),
            "CC_HARNESS_TASK_ID": str(context.task.task_id),
            "CC_HARNESS_TERMINAL_AGENT_RUNTIME": "1",
        }
        if terminal_timeout is not None:
            terminal_agent_env["CC_HARNESS_TASK_TIMEOUT_SECONDS"] = str(terminal_timeout)
        command = build_harbor_command(
            uvx=str(shutil.which("uvx") or "uvx"),
            project_root=context.project_root,
            dataset=self.dataset,
            task_name=str(context.task.payload.get("harbor_task_name") or context.task.task_id),
            harness=HarnessKind.CC_HARNESS,
            wheel_path=self._run_wheel(context),
            uv_bootstrap_path=(
                self._run_uv_bootstrap(context)
                if self.slug.startswith("terminal-bench")
                else None
            ),
            tiktoken_bootstrap_path=(
                self._run_tiktoken_bootstrap(context)
                if self.slug.startswith("terminal-bench")
                else None
            ),
            env_file=env_file,
            jobs_dir=jobs,
            agent_env=(terminal_agent_env if self.slug.startswith("terminal-bench") else None),
            extra_docker_compose_paths=(
                (terminal_overlay,) if terminal_overlay is not None else None
            ),
        )
        if self.slug == "terminal-bench-2.1" and terminal_overlay is not None:
            _assert_official_terminal_launch(command, terminal_overlay)
            (context.attempt_root / "official-protocol.json").write_text(
                json.dumps(
                    {
                        "dataset": self.dataset,
                        "harbor_version": HARBOR_VERSION,
                        "official_task_and_verifier_unchanged": True,
                        "custom_agent_overlay": str(terminal_overlay),
                        "custom_agent_overlay_scope": "agent-runtime-only",
                        "n_attempts": 1,
                        "leaderboard_deviation": "single trial instead of five",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        environment = dict(os.environ)
        if self.slug.startswith("terminal-bench"):
            for secret_name in (
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
            ):
                environment.pop(secret_name, None)
        environment.update(launch_values)
        environment["PYTHONUTF8"] = "1"
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(context.project_root), existing_pythonpath) if value
        )
        (context.attempt_root / "command.json").write_text(
            json.dumps(command, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=context.project_root,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
            start_new_session=os.name != "nt",
        )
        communicate = asyncio.create_task(process.communicate())
        started = time.monotonic()
        try:
            while not communicate.done():
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate), timeout=_TERMINAL_HEARTBEAT_SECONDS
                    )
                except TimeoutError:
                    elapsed = int(time.monotonic() - started)
                    activity = _process_activity_snapshot(process.pid, jobs)
                    agent_activity = _terminal_agent_progress_snapshot(jobs)
                    context.progress(
                        "[terminal-bench] "
                        f"task={context.task_index}/{context.task_total} "
                        f"task_id={context.task.task_id} phase=harbor "
                        f"elapsed={_duration(elapsed)} pid={process.pid} "
                        f"process={'running' if process.returncode is None else 'exited'} "
                        f"activity=cpu:{activity['cpu_ticks']} children:{activity['children']} "
                        f"io:{activity['io_bytes']} sockets:{activity['network_sockets']} "
                        f"files:{activity['workspace_files']} "
                        f"agent_progress:bytes={agent_activity['agent_progress_bytes']} "
                        f"mtime_ns={agent_activity['agent_progress_mtime_ns']} "
                        f"files={agent_activity['agent_progress_files']} "
                        f"last={agent_activity['agent_last_event']} "
                        "event=heartbeat waiting for agent/runtime"
                    )
                    if elapsed >= context.watchdog_seconds:
                        raise TimeoutError
            stdout, stderr = await communicate
        except TimeoutError:
            await _terminate_process_tree(process)
            return TrialOutcome(
                # An external watchdog is a launcher/runtime boundary, not an
                # official task assertion. Preserve it as pending evidence;
                # do not turn a silent process into a zero-reward task fail.
                status=TrialStatus.INVALID,
                invalid_reason="Harbor exceeded the external emergency watchdog",
                protocol={
                    "exception_is_infrastructure": True,
                    "emergency_watchdog": True,
                    # The watchdog fires after a process has been given the
                    # full liveness window; there is no proof this happened
                    # before a model request. Never replay it automatically.
                    "model_phase_started": True,
                    "transient_infrastructure": False,
                    "official_error_counted_as_zero": False,
                },
            )
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            raise
        (context.attempt_root / "harbor.stdout.txt").write_bytes(stdout)
        (context.attempt_root / "harbor.stderr.txt").write_bytes(stderr)
        if jobs != evidence_jobs:
            if evidence_jobs.exists():
                shutil.rmtree(evidence_jobs)
            shutil.copytree(jobs, evidence_jobs)
            jobs = evidence_jobs
        docker_after = _docker_snapshot()
        (context.attempt_root / "docker-after.json").write_text(
            json.dumps(docker_after, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        all_job_roots = sorted(
            path.parent for path in jobs.rglob("result.json") if path.parent.parent == jobs
        )
        if not all_job_roots:
            diagnostic = stderr.decode("utf-8", errors="replace")[-4_000:]
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=(
                    f"Harbor produced no auditable job (exit={process.returncode}): {diagnostic}"
                ),
                protocol={
                    "launcher_failure": True,
                    "exception_is_infrastructure": True,
                    "transient_infrastructure": _transient_text(diagnostic),
                    "environment_not_ready": _environment_not_ready_text(diagnostic),
                    "official_error_counted_as_zero": False,
                },
            )
        # A resumed Harbor task can retain a completed/interrupted job and
        # append a fresh job under the same jobs directory.  Multiple jobs are
        # valid resume evidence; grade only the newest completed job and retain
        # the older jobs for audit/recovery instead of converting the task into
        # a deterministic infrastructure failure.
        job_roots = [
            max(
                all_job_roots,
                key=lambda path: (
                    path.joinpath("result.json").stat().st_mtime_ns,
                    str(path),
                ),
            )
        ]
        retained_job_count = len(all_job_roots)
        job = json.loads((job_roots[0] / "result.json").read_text(encoding="utf-8"))
        trial_roots = sorted(
            path
            for path in job_roots[0].iterdir()
            if path.is_dir() and (path / "result.json").is_file()
        )
        trial_result = (
            json.loads((trial_roots[0] / "result.json").read_text(encoding="utf-8"))
            if len(trial_roots) == 1
            else {}
        )
        stats = job.get("stats") or {}
        if int(stats.get("n_errored_trials") or 0):
            serialized = _harbor_failure_diagnostic(job_roots[0], job, trial_result)
            _write_failure_diagnostic(context.attempt_root, serialized)
            exception_info = trial_result.get("exception_info") or {}
            exception_type = (
                str(exception_info.get("exception_type"))
                if isinstance(exception_info, Mapping)
                else None
            )
            terminal_grade = (
                _terminal_bench_errored_grade(stats, trial_result)
                if self.slug == "terminal-bench-2.1"
                else None
            )
            if terminal_grade is not None:
                status, reward = terminal_grade
                usage = _harbor_usage(stats, trial_result)
                verifier_diagnostic = _verifier_failure_diagnostic(job_roots[0])
                failure_class = _terminal_failure_class(
                    trial_result, verifier_diagnostic=verifier_diagnostic
                )
                status = (
                    _terminal_official_zero_reward_status(failure_class)
                    if reward <= 0
                    else status
                )
                return TrialOutcome(
                    status=status,
                    metrics={
                        "reward": reward,
                        "errored_trial": 1,
                        "agent_timed_out": int(exception_type == "AgentTimeoutError"),
                    },
                    failure_reason=(
                        "official Harbor grader rejected the solution"
                        if status is TrialStatus.FAIL
                        else None
                    ),
                    invalid_reason=(
                        "official verifier did not execute because its infrastructure failed"
                        if status is TrialStatus.INVALID
                        else None
                    ),
                    usage=usage,
                    protocol={
                        "dataset": self.dataset,
                        "harbor_version": HARBOR_VERSION,
                        "harbor_job": job_roots[0]
                        .relative_to(context.attempt_root)
                        .as_posix(),
                        "harbor_job_count": retained_job_count,
                        "harbor_resume_selection": "newest-top-level-job",
                        "harbor_exception_type": exception_type,
                        "agent_lifecycle_error": True,
                        "deterministic_verifier_reward_preserved": True,
                        "usage_telemetry_incomplete": _usage_telemetry_incomplete(usage),
                        "model_phase_started": bool(usage.get("model_phase_started")),
                        "failure_class": failure_class,
                        "verifier_infrastructure": (
                            failure_class in {"verifier_infrastructure", "mixed"}
                        ),
                        "exception_is_infrastructure": status is TrialStatus.INVALID,
                        "transient_infrastructure": (
                            transient_infrastructure_text(verifier_diagnostic)
                            if status is TrialStatus.INVALID
                            else False
                        ),
                        "failure_diagnostic": verifier_diagnostic[-32_000:],
                        "official_error_counted_as_zero": False,
                    },
                )
            reason = (
                "Harbor trial errored before a deterministic grade"
                + (f" ({exception_type})" if exception_type else "")
            )
            verifier_diagnostic = _verifier_failure_diagnostic(job_roots[0])
            failure_class = _terminal_failure_class(
                trial_result, verifier_diagnostic=verifier_diagnostic
            )
            terminal_official = self.slug == "terminal-bench-2.1"
            terminal_status = (
                _terminal_official_zero_reward_status(failure_class)
                if terminal_official
                else TrialStatus.INVALID
            )
            usage = _harbor_usage(stats, trial_result)
            return TrialOutcome(
                status=terminal_status,
                metrics=(
                    {"reward": 0.0, "errored_trial": 1}
                    if terminal_official and terminal_status is TrialStatus.FAIL
                    else {}
                ),
                failure_reason=(
                    reason if terminal_official and terminal_status is TrialStatus.FAIL else None
                ),
                invalid_reason=(
                    "official verifier did not execute because its infrastructure failed"
                    if terminal_official and terminal_status is TrialStatus.INVALID
                    else (None if terminal_official else reason)
                ),
                usage=usage,
                protocol={
                    "harbor_job": job_roots[0].relative_to(context.attempt_root).as_posix(),
                    "harbor_exception_type": exception_type,
                    "failure_diagnostic": serialized[-32_000:],
                    "failure_class": failure_class,
                    "model_phase_started": bool(usage.get("model_phase_started")),
                    "verifier_infrastructure": (
                        failure_class in {"verifier_infrastructure", "mixed"}
                    ),
                    "exception_is_infrastructure": terminal_status is TrialStatus.INVALID,
                    "transient_infrastructure": _transient_text(serialized),
                    "environment_not_ready": _environment_not_ready_text(serialized),
                    "official_error_counted_as_zero": (
                        terminal_official and terminal_status is TrialStatus.FAIL
                    ),
                },
            )
        reward = _reward(stats)
        if reward is None:
            serialized = _harbor_failure_diagnostic(job_roots[0], job, trial_result)
            _write_failure_diagnostic(context.attempt_root, serialized)
            usage = _harbor_usage(stats, trial_result)
            return TrialOutcome(
                # Without a deterministic reward there is no official
                # assertion to grade. Keep it as infrastructure evidence for
                # a checkpoint-aware retry rather than inventing reward=0.
                status=TrialStatus.INVALID,
                metrics={},
                invalid_reason=(
                    "Harbor result does not contain a deterministic reward"
                ),
                usage=usage,
                protocol={
                    "exception_is_infrastructure": True,
                    "failure_diagnostic": serialized[-32_000:],
                    "transient_infrastructure": _transient_text(serialized),
                    "environment_not_ready": _environment_not_ready_text(serialized),
                    "model_phase_started": bool(usage.get("model_phase_started")),
                    "official_error_counted_as_zero": False,
                },
            )
        verifier_diagnostic = (
            _verifier_failure_diagnostic(job_roots[0]) if reward <= 0 else ""
        )
        failure_class = (
            _terminal_failure_class(trial_result, verifier_diagnostic=verifier_diagnostic)
            if reward <= 0 and self.slug == "terminal-bench-2.1"
            else None
        )
        if verifier_diagnostic:
            _write_failure_diagnostic(context.attempt_root, verifier_diagnostic)
        usage = _harbor_usage(stats, trial_result)
        status = (
            TrialStatus.PASS
            if reward > 0
            else _terminal_official_zero_reward_status(failure_class)
        )
        return TrialOutcome(
            status=status,
            metrics={"reward": reward},
            usage=usage,
            failure_reason=(
                "official Harbor grader rejected the solution"
                if status is TrialStatus.FAIL
                else None
            ),
            invalid_reason=(
                "official verifier did not execute because its infrastructure failed"
                if status is TrialStatus.INVALID
                else None
            ),
            protocol={
                "dataset": self.dataset,
                "harbor_version": HARBOR_VERSION,
                "harbor_job": job_roots[0].relative_to(context.attempt_root).as_posix(),
                "harbor_job_count": retained_job_count,
                "harbor_resume_selection": "newest-top-level-job",
                "failure_class": failure_class,
                "verifier_infrastructure": (
                    failure_class in {"verifier_infrastructure", "mixed"}
                ),
                "exception_is_infrastructure": status is TrialStatus.INVALID,
                "model_phase_started": bool(usage.get("model_phase_started")),
                "transient_infrastructure": (
                    transient_infrastructure_text(verifier_diagnostic)
                    if status is TrialStatus.INVALID
                    else False
                ),
                "failure_diagnostic": verifier_diagnostic[-32_000:],
            },
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        rewards = [
            float((outcome.get("metrics") or {}).get("reward"))
            for outcome in outcomes
            if isinstance((outcome.get("metrics") or {}).get("reward"), (int, float))
        ]
        return {
            "resolved_rate": sum(value > 0 for value in rewards) / len(rewards) if rewards else None,
            "graded_task_count": len(rewards),
            "leaderboard_compatible": False,
            "trials_per_task": 1,
        }

    def _wheel(self, project_root: Path) -> Path:
        return (self.wheel_path or project_root / "eval" / "result" / "cc-only" / "_artifacts" / "cc_harness-0.1.0-py3-none-any.whl").resolve()

    def _run_wheel(self, context: TrialContext) -> Path:
        frozen = context.output_root / "frozen-inputs" / "cc_harness-0.1.0-py3-none-any.whl"
        return frozen if frozen.is_file() else self._wheel(context.project_root)


class SweBenchVerifiedAdapter(_HarborAdapter):
    slug = "swe-bench-verified"
    title = "SWE-bench Verified"
    protocol_version = "swe-bench-verified-cc-only.v1"
    dataset = SWEBENCH_DATASET
    adaptations = ("The 50-task portfolio is not the complete 500-task official benchmark.",)

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        source = json.loads((project_root / "eval" / "harbor" / "catalogs" / "swebench_verified_500.json").read_text(encoding="utf-8"))
        records = source["tasks"]
        names = [item["name"] for item in records] if profile is EvalProfile.FULL else list(_SWEBENCH_PORTFOLIO)
        refs = {item["name"]: item["ref"] for item in records}
        return tuple(
            BenchmarkTask(task_id=name, group=_swe_repo(name), payload={"ref": refs[name], "harbor_task_name": name})
            for name in names
        )


class TerminalBenchAdapter(_HarborAdapter):
    slug = "terminal-bench-2.1"
    title = "Terminal-Bench 2.1"
    protocol_version = "terminal-bench-2.1-official-single-trial.v1"
    dataset = TERMINAL_BENCH_21_DATASET
    max_automatic_attempts = 1
    # This is separate from the one official model attempt.  Only a
    # pre-model transient launcher/Docker/network failure may consume these
    # checkpoint-reused infrastructure retries.
    max_infrastructure_attempts = 10
    adaptations = (
        "Each task runs once, while leaderboard submissions require at least five trials per task.",
        "Pre-model transient launcher failures may retry up to ten times on the same checkpoint; model-bearing failures are never replayed automatically.",
    )

    def check(
        self,
        project_root: Path,
        profile: EvalProfile,
        tasks: Sequence[BenchmarkTask],
    ) -> CheckResult:
        base = super().check(project_root, profile, tasks)
        docker_health = _docker_healthcheck()
        docker_ready = bool(docker_health["ready"])
        storage_path = _docker_storage_path()
        free_bytes = shutil.disk_usage(storage_path).free if storage_path is not None else None
        disk_ready = free_bytes is None or free_bytes >= _TERMINAL_MIN_FREE_BYTES
        catalog_ready = profile is not EvalProfile.FULL or len(tasks) == 89
        bootstrap = uv_bootstrap_cache_path(project_root)
        bootstrap_error: str | None = None
        if not bootstrap.is_file():
            try:
                bootstrap = ensure_uv_bootstrap(project_root)
            except RuntimeError as exc:
                bootstrap_error = str(exc)
        bootstrap_ready = False
        if bootstrap.is_file():
            try:
                bootstrap_ready = (
                    uv_bootstrap_identity(bootstrap)["sha256"] == UV_BOOTSTRAP_SHA256
                )
            except (OSError, ValueError):
                bootstrap_ready = False
        tiktoken_bootstrap = tiktoken_bootstrap_cache_path(project_root)
        tiktoken_bootstrap_error: str | None = None
        try:
            tiktoken_bootstrap = ensure_tiktoken_bootstrap(project_root)
        except RuntimeError as exc:
            tiktoken_bootstrap_error = str(exc)
        tiktoken_bootstrap_ready = False
        if tiktoken_bootstrap.is_file():
            try:
                tiktoken_bootstrap_ready = bool(
                    tiktoken_bootstrap_identity(tiktoken_bootstrap)
                )
            except (OSError, ValueError):
                tiktoken_bootstrap_ready = False
        host_agent_import_ready, host_agent_import_error = _harbor_host_import_check(
            project_root
        )
        agent_runtime_ready = False
        agent_runtime_error: str | None = None
        protocol_audit: dict[str, Any] = {}
        try:
            runtime = ensure_verifier_runtime(project_root)
            with tempfile.TemporaryDirectory(prefix="cc-harness-agent-overlay-") as temp:
                overlay = agent_runtime_overlay(
                    project_root, Path(temp), runtime_path=runtime
                )
                audit_command = [
                    "harbor",
                    "run",
                    "--dataset",
                    TERMINAL_BENCH_21_DATASET,
                    "--agent",
                    "harbor_plugins.cc_harness_agent:CCHarnessHarborAgent",
                    "--n-attempts",
                    "1",
                    "--extra-docker-compose",
                    str(overlay),
                ]
                _assert_official_terminal_launch(audit_command, overlay)
                protocol_audit = {
                    "status": "pass",
                    "official_task_and_verifier_unchanged": True,
                    "custom_agent_overlay_scope": "agent-runtime-only",
                    "forbidden_command_replacements": [],
                }
            agent_runtime_ready = True
        except (OSError, RuntimeError, ValueError) as exc:
            agent_runtime_error = str(exc)
            protocol_audit = {"status": "fail", "error": agent_runtime_error}
        terminal_host = inspect_terminal_host(project_root)
        details = dict(base.details)
        details.update(
            {
                "official_dataset": TERMINAL_BENCH_21_DATASET,
                "official_success_rule": "reward > 0",
                "official_denominator": 89,
                "docker_daemon_ready": docker_ready,
                "docker_healthcheck": docker_health,
                "docker_storage_path": str(storage_path) if storage_path else None,
                "docker_free_bytes": free_bytes,
                "minimum_free_bytes": _TERMINAL_MIN_FREE_BYTES,
                "catalog_complete": catalog_ready,
                "concurrency": 1,
                "mode": "coding",
                "long_term_memory": False,
                "linux_uv_bootstrap": str(bootstrap),
                "linux_uv_bootstrap_ready": bootstrap_ready,
                "linux_uv_bootstrap_error": bootstrap_error,
                "official_verifier_unmodified": agent_runtime_ready,
                "custom_agent_runtime_ready": agent_runtime_ready,
                "custom_agent_runtime_error": agent_runtime_error,
                "formal_protocol_audit": protocol_audit,
                "offline_tiktoken_bootstrap": str(tiktoken_bootstrap),
                "offline_tiktoken_bootstrap_ready": tiktoken_bootstrap_ready,
                "offline_tiktoken_bootstrap_error": tiktoken_bootstrap_error,
                "host_agent_import_ready": host_agent_import_ready,
                "host_agent_import_error": host_agent_import_error,
                "execution_backend": terminal_host,
            }
        )
        warnings = list(base.warnings)
        if not docker_ready:
            warnings.append("Docker daemon is unavailable")
        if not disk_ready:
            warnings.append("Docker storage has less than 80 GB free")
        if not catalog_ready:
            warnings.append("full profile does not resolve exactly 89 official tasks")
        if not bootstrap_ready:
            warnings.append(
                "verified Linux uv bootstrap is missing; run Terminal-Bench preparation"
            )
            if bootstrap_error:
                warnings.append(bootstrap_error)
        if not tiktoken_bootstrap_ready:
            warnings.append(
                "offline Terminal-Bench tokenizer bootstrap is missing; run Terminal-Bench preparation"
            )
            if tiktoken_bootstrap_error:
                warnings.append(tiktoken_bootstrap_error)
        if not host_agent_import_ready:
            warnings.append(
                "Harbor cannot import the cc-harness agent with its pinned host environment"
            )
            if host_agent_import_error:
                warnings.append(host_agent_import_error)
        if not agent_runtime_ready:
            warnings.append("formal custom-agent runtime protocol audit failed")
            if agent_runtime_error:
                warnings.append(agent_runtime_error)
        if not terminal_host["ready"]:
            warnings.append(
                "Terminal-Bench requires Ubuntu WSL2 native Docker: "
                + ", ".join(terminal_host["errors"])
            )
        return CheckResult(
            ready=(
                base.ready
                and docker_ready
                and disk_ready
                and catalog_ready
                and bootstrap_ready
                and tiktoken_bootstrap_ready
                and host_agent_import_ready
                and agent_runtime_ready
                and terminal_host["ready"]
            ),
            details=details,
            warnings=tuple(warnings),
        )

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        metadata_path = (
            project_root / "eval" / "harbor" / "catalogs" / "terminal_bench_2_1.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("dataset") != TERMINAL_BENCH_21_DATASET:
            raise ValueError("Terminal-Bench 2.1 catalog dataset identity is stale")
        records = {item["name"]: item for item in metadata.get("tasks") or []}
        if set(records) != set(_TERMINAL_ALL):
            raise ValueError("Terminal-Bench 2.1 catalog does not contain the canonical 89 tasks")
        names = _TERMINAL_ALL if profile is EvalProfile.FULL else _TERMINAL_PORTFOLIO
        return tuple(
            BenchmarkTask(
                task_id=f"terminal-bench/{name}",
                group=records[name].get("category"),
                payload={
                    "harbor_task_name": f"terminal-bench/{name}",
                    "difficulty": records[name].get("difficulty"),
                    "source_digest": records[name].get("source_digest"),
                },
            )
            for name in names
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        rewards = [
            float((outcome.get("metrics") or {}).get("reward") or 0)
            for outcome in outcomes
        ]
        successes = sum(reward > 0 for reward in rewards)
        by_category: dict[str, dict[str, int | float]] = {}
        for outcome in outcomes:
            group = str(outcome.get("group") or "uncategorized")
            bucket = by_category.setdefault(group, {"trials": 0, "successes": 0})
            bucket["trials"] = int(bucket["trials"]) + 1
            reward = float((outcome.get("metrics") or {}).get("reward") or 0)
            bucket["successes"] = int(bucket["successes"]) + int(reward > 0)
        for bucket in by_category.values():
            bucket["accuracy"] = int(bucket["successes"]) / int(bucket["trials"])
        return {
            "official_success_rule": "reward > 0",
            "official_denominator": 89,
            "successful_tasks": successes,
            "single_pass_accuracy": successes / 89,
            "terminal_results": len(outcomes),
            "leaderboard_compatible": False,
            "trials_per_task": 1,
            "by_category": by_category,
        }

    def run_identity(self, project_root: Path) -> Mapping[str, Any]:
        from ..terminal_prewarm import terminal_plugin_digest

        wheel = self._wheel(project_root)
        bootstrap = uv_bootstrap_cache_path(project_root)
        tiktoken_bootstrap = tiktoken_bootstrap_cache_path(project_root)
        git_head = _git_output(project_root, "rev-parse", "HEAD")
        dirty = _git_output(project_root, "status", "--porcelain=v1", "--untracked-files=all")
        return {
            "dataset": self.dataset,
            "harbor_version": HARBOR_VERSION,
            "wheel_sha256": _sha256(wheel) if wheel.is_file() else None,
            "harbor_plugin_sha256": terminal_plugin_digest(project_root),
            "linux_uv_bootstrap": (
                uv_bootstrap_identity(bootstrap) if bootstrap.is_file() else None
            ),
            "official_verifier_unmodified": True,
            "offline_tiktoken_bootstrap": (
                tiktoken_bootstrap_identity(tiktoken_bootstrap)
                if tiktoken_bootstrap.is_file()
                else None
            ),
            "git_head": git_head,
            "git_dirty_digest": (
                "sha256:" + hashlib.sha256(dirty.encode("utf-8")).hexdigest()
                if dirty
                else None
            ),
            "mode": "coding",
            "thinking": "provider-default",
            "concurrency": 1,
            "trials_per_task": 1,
            "long_term_memory": False,
            "timeout_multiplier": None,
            "agent_timeout_multiplier": None,
            "agent_setup_timeout_multiplier": None,
            "environment_build_timeout_multiplier": None,
            "verifier_timeout_multiplier": None,
            "max_iterations": _TERMINAL_MAX_ITERATIONS,
            "network_transport": os.environ.get(
                "CC_HARNESS_TERMINAL_NETWORK_TRANSPORT", "direct"
            ),
            "execution_backend": inspect_terminal_host(project_root),
        }

    def freeze_inputs(self, project_root: Path, output_root: Path) -> None:
        source = self._wheel(project_root)
        bootstrap = ensure_uv_bootstrap(project_root)
        tiktoken_bootstrap = ensure_tiktoken_bootstrap(project_root)
        target = output_root / "frozen-inputs" / "cc_harness-0.1.0-py3-none-any.whl"
        if target.is_file():
            if _sha256(target) != _sha256(source):
                if os.environ.get("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH") != "1":
                    raise ValueError(
                        "frozen Terminal-Bench wheel differs from the prepared artifact"
                    )
                superseded = output_root / "frozen-inputs" / "superseded"
                superseded.mkdir(parents=True, exist_ok=True)
                old_digest = _sha256(target)
                shutil.copy2(
                    target,
                    superseded / f"cc_harness-0.1.0-py3-none-any.{old_digest[7:19]}.whl",
                )
                temporary = target.with_suffix(".whl.refreshing")
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".whl.copying")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        bootstrap_target = output_root / "frozen-inputs" / bootstrap.name
        if bootstrap_target.is_file():
            if _sha256(bootstrap_target) != _sha256(bootstrap):
                raise ValueError("frozen Linux uv bootstrap differs from the prepared artifact")
        else:
            temporary_bootstrap = bootstrap_target.with_suffix(".copying")
            shutil.copyfile(bootstrap, temporary_bootstrap)
            os.replace(temporary_bootstrap, bootstrap_target)
        tiktoken_target = output_root / "frozen-inputs" / tiktoken_bootstrap.name
        if tiktoken_target.is_file():
            if _sha256(tiktoken_target) != _sha256(tiktoken_bootstrap):
                raise ValueError(
                    "frozen Terminal-Bench tiktoken bootstrap differs from the prepared artifact"
                )
        else:
            temporary_tiktoken = tiktoken_target.with_suffix(".copying")
            shutil.copyfile(tiktoken_bootstrap, temporary_tiktoken)
            os.replace(temporary_tiktoken, tiktoken_target)

    def _run_uv_bootstrap(self, context: TrialContext) -> Path:
        frozen = context.output_root / "frozen-inputs" / uv_bootstrap_cache_path(
            context.project_root
        ).name
        if not frozen.is_file():
            raise FileNotFoundError(f"frozen Linux uv bootstrap is missing: {frozen}")
        return frozen

    def _run_tiktoken_bootstrap(self, context: TrialContext) -> Path:
        frozen = context.output_root / "frozen-inputs" / tiktoken_bootstrap_cache_path(
            context.project_root
        ).name
        if not frozen.is_file():
            raise FileNotFoundError(f"frozen tiktoken bootstrap is missing: {frozen}")
        return frozen

    def after_attempt(self, context: TrialContext, outcome: TrialOutcome) -> None:
        del outcome
        before_path = context.attempt_root / "docker-before.json"
        if not before_path.is_file():
            return
        before = json.loads(before_path.read_text(encoding="utf-8"))
        after = _docker_snapshot()
        cleanup = _cleanup_owned_harbor_resources(before, after)
        (context.attempt_root / "docker-cleanup.json").write_text(
            json.dumps(cleanup, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class TerminalBench20Adapter(TerminalBenchAdapter):
    slug = "terminal-bench-2.0"
    title = "Terminal-Bench 2.0 (legacy)"
    protocol_version = "terminal-bench-2.0-single-pass.v1"
    dataset = TERMINAL_BENCH_20_DATASET
    adaptations = (
        "Legacy Terminal-Bench 2.0 evidence is version-specific and cannot be pooled with 2.1.",
        "Each task runs once, while leaderboard submissions require repeated trials.",
    )

    def catalog(
        self, project_root: Path, profile: EvalProfile
    ) -> Sequence[BenchmarkTask]:
        del project_root
        names = _TERMINAL_ALL if profile is EvalProfile.FULL else _TERMINAL_PORTFOLIO
        return tuple(
            BenchmarkTask(
                task_id=f"terminal-bench/{name}",
                payload={"harbor_task_name": name},
            )
            for name in names
        )


_TERMINAL_ALL = _TERMINAL_PORTFOLIO + (
    "gpt2-codegolf", "break-filter-js-from-html", "write-compressor", "merge-diff-arc-agi-task",
    "winning-avg-corewars", "log-summary-date-ranges", "pytorch-model-cli", "path-tracing-reverse",
    "regex-chess", "path-tracing", "prove-plus-comm", "feal-linear-cryptanalysis", "caffe-cifar-10",
    "distribution-search", "mteb-retrieve", "pypi-server", "custom-memory-heap-crash",
    "adaptive-rejection-sampler", "multi-source-data-merger", "chess-best-move", "overfull-hbox",
    "polyglot-rust-c", "hf-model-inference", "headless-terminal", "schemelike-metacircular-eval",
    "qemu-startup", "git-multibranch", "kv-store-grpc", "install-windows-3.11", "make-doom-for-mips",
    "torch-pipeline-parallelism", "tune-mjcf", "gcode-to-text", "make-mips-interpreter",
    "count-dataset-tokens", "circuit-fibsqrt", "mteb-leaderboard", "query-optimize",
    "financial-document-processor", "regex-log", "filter-js-from-html",
    "feal-differential-cryptanalysis", "polyglot-c-py", "cancel-async-tasks", "bn-fit-modify",
    "fix-ocaml-gc", "model-extraction-relu-logits", "sparql-university", "large-scale-text-editing",
    "sqlite-db-truncate", "sanitize-git-repo", "build-pmars", "rstan-to-pystan", "sqlite-with-gcov",
    "openssl-selfsigned-cert", "constraints-scheduling", "dna-insert", "vulnerable-secret", "dna-assembly",
)


def _verifier_reward(trial_result: Mapping[str, Any]) -> float | None:
    verifier_result = trial_result.get("verifier_result") or {}
    rewards = verifier_result.get("rewards") or {}
    reward = rewards.get("reward")
    return float(reward) if isinstance(reward, (int, float)) else None


def _terminal_bench_errored_grade(
    stats: Mapping[str, Any], trial_result: Mapping[str, Any]
) -> tuple[TrialStatus, float] | None:
    """Preserve a deterministic official grade despite an agent lifecycle error."""

    reward = _verifier_reward(trial_result)
    if reward is None:
        reward = _reward(stats)
    if reward is None:
        return None
    return (TrialStatus.PASS if reward > 0 else TrialStatus.FAIL, reward)


def _reward(stats: Mapping[str, Any]) -> float | None:
    for evaluation in (stats.get("evals") or {}).values():
        metrics = evaluation.get("metrics") or []
        if metrics and isinstance(metrics[0].get("mean"), (int, float)):
            return float(metrics[0]["mean"])
    return None


def _harbor_usage(
    stats: Mapping[str, Any], trial_result: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    input_tokens = int(stats.get("n_input_tokens") or 0)
    cached_tokens = int(stats.get("n_cache_tokens") or 0)
    output_tokens = int(stats.get("n_output_tokens") or 0)
    metadata = ((trial_result or {}).get("agent_result") or {}).get("metadata") or {}
    model_calls = _nonnegative_int(metadata.get("model_calls"))
    tool_calls = _nonnegative_int(metadata.get("tool_calls"))
    if model_calls == 0 or tool_calls == 0:
        exception = (trial_result or {}).get("exception_info") or {}
        serialized = json.dumps(exception, ensure_ascii=False)
        model_calls = model_calls or _embedded_usage_count(serialized, "model_calls")
        tool_calls = tool_calls or _embedded_usage_count(serialized, "tool_calls")
    raw_cost = metadata.get("api_reported_cost")
    try:
        api_reported_cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        api_reported_cost = None
    if api_reported_cost is not None and (
        not math.isfinite(api_reported_cost) or api_reported_cost < 0
    ):
        api_reported_cost = None
    raw_currency = metadata.get("api_reported_cost_currency")
    api_reported_cost_currency = (
        str(raw_currency).strip().upper() if raw_currency is not None else None
    )
    raw_status = metadata.get("api_cost_status")
    api_cost_status = (
        str(raw_status).strip().lower() if raw_status is not None else None
    )
    if api_cost_status is None and api_reported_cost is not None:
        api_cost_status = "reported"
    # Some Harbor adapters expose token usage before they populate their
    # optional model-call counter. Treat that usage as evidence that a
    # provider call happened; otherwise a missing direct price could be
    # incorrectly reported as ``unavailable`` instead of ``incomplete``.
    api_cost_observed = (
        bool(metadata.get("api_cost_observed"))
        or model_calls > 0
        or input_tokens > 0
        or output_tokens > 0
    )
    api_cost_complete = (
        api_cost_status == "reported" and api_reported_cost is not None
    )
    if not api_cost_complete:
        api_cost_status = "incomplete" if api_cost_observed else "unavailable"
    provider_cost_microusd = (
        round(api_reported_cost * 1_000_000)
        if api_cost_complete and api_reported_cost_currency in (None, "USD")
        else None
    )
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": max(0, input_tokens - cached_tokens),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        # ``stats.cost_usd`` is not an auditable provider fact by itself; it
        # may be a framework fallback.  Use only the explicit direct-cost
        # fields emitted by the cc-harness result envelope.
        "cost_microusd": provider_cost_microusd,
        "api_reported_cost": api_reported_cost if api_cost_complete else None,
        "api_reported_cost_currency": api_reported_cost_currency,
        "api_cost_source": "provider",
        "api_cost_status": api_cost_status,
        "api_cost_observed": api_cost_observed,
        "api_cost_complete": api_cost_complete,
        "model_calls": model_calls,
        # Token counters are evidence that the model phase began, not an
        # inferred number of calls.  The runner uses this boolean solely to
        # prevent replaying a paid attempt when Harbor omitted its call count.
        "model_phase_started": bool(model_calls or input_tokens or output_tokens),
        "tool_calls": tool_calls,
        "wall_time_ms": 0,
    }


def _usage_telemetry_incomplete(usage: Mapping[str, Any]) -> bool:
    """Tell the protocol when model/tool usage itself was not observable."""

    return not any(
        int(usage.get(name) or 0)
        for name in (
            "model_calls",
            "tool_calls",
            "input_tokens",
            "output_tokens",
        )
    )


def _harbor_failure_diagnostic(
    job_root: Path,
    job: Mapping[str, Any],
    trial_result: Mapping[str, Any],
) -> str:
    """Collect nested Harbor evidence before classifying an errored trial."""

    parts = [
        json.dumps(
            {
                "job_stats": job.get("stats") or {},
                "exception_info": trial_result.get("exception_info") or {},
                "verifier_result": trial_result.get("verifier_result") or {},
            },
            ensure_ascii=False,
        )
    ]
    evidence_names = (
        "exception.txt",
        "trial.log",
        "agent/cc-harness.stderr",
        "agent/cc-harness.jsonl",
        "verifier/test-stdout.txt",
        "verifier/test-stderr.txt",
    )
    for trial_dir in sorted(path for path in job_root.iterdir() if path.is_dir()):
        for relative in evidence_names:
            path = trial_dir / relative
            if not path.is_file():
                continue
            try:
                parts.append(
                    f"--- {path.relative_to(job_root).as_posix()} ---\n{_read_tail(path)}"
                )
            except OSError:
                continue
    return "\n".join(parts)[-128_000:]


def _verifier_failure_diagnostic(job_root: Path) -> str:
    """Read verifier-only evidence so agent errors cannot spoof attribution."""

    parts: list[str] = []
    for trial_dir in sorted(path for path in job_root.iterdir() if path.is_dir()):
        for relative in ("verifier/test-stdout.txt", "verifier/test-stderr.txt"):
            path = trial_dir / relative
            if not path.is_file():
                continue
            try:
                parts.append(f"--- {relative} ---\n{_read_tail(path)}")
            except OSError:
                continue
    return "\n".join(parts)[-64_000:]


def _terminal_failure_class(
    trial_result: Mapping[str, Any], *, verifier_diagnostic: str
) -> str:
    """Attribute a zero reward without changing the official reward/status."""

    exception = trial_result.get("exception_info") or {}
    agent_error = bool(
        isinstance(exception, Mapping)
        and (exception.get("exception_type") or exception.get("exception_message"))
    )
    verifier_infrastructure = verifier_bootstrap_failure_text(verifier_diagnostic)
    if verifier_infrastructure and agent_error:
        return "mixed"
    if verifier_infrastructure:
        return "verifier_infrastructure"
    if agent_error:
        exception_text = " ".join(
            str(exception.get(name) or "")
            for name in ("exception_type", "exception_message")
        )
        if transient_infrastructure_text(exception_text):
            return "provider_transport"
        return "agent_runtime"
    return "task_failure"


def _terminal_official_zero_reward_status(failure_class: str | None) -> TrialStatus:
    """Keep verifier outages out of the benchmark denominator.

    The official reward evidence remains ``0`` in the retained Harbor job.  A
    verifier that never reached an assertion, however, is operational evidence
    rather than a task grade and must enter the runner's infrastructure resume
    path instead of being published as a solution failure.
    """

    if failure_class in {
        "verifier_infrastructure",
        "mixed",
        "provider_transport",
    }:
        return TrialStatus.INVALID
    return TrialStatus.FAIL


def _write_failure_diagnostic(attempt_root: Path, diagnostic: str) -> None:
    try:
        (attempt_root / "harbor-failure-diagnostic.txt").write_text(
            diagnostic,
            encoding="utf-8",
        )
    except OSError:
        return


def _read_tail(path: Path, maximum_bytes: int = 32 * 1024) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - maximum_bytes))
        return stream.read(maximum_bytes).decode("utf-8", errors="replace")


def _nonnegative_int(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _embedded_usage_count(text: str, field: str) -> int:
    matches = re.findall(rf'\\?"{re.escape(field)}\\?"\s*:\s*(\d+)', text)
    return int(matches[-1]) if matches else 0


def _swe_repo(name: str) -> str:
    return name.split("/", 1)[1].split("__", 1)[0]


def _duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _git_output(project_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _transient_text(text: str) -> bool:
    return transient_infrastructure_text(text)


def _environment_not_ready_text(text: str) -> bool:
    normalized = text.casefold()
    return any(
        marker in normalized
        for marker in (
            "modulenotfounderror",
            "no module named",
            "verifier test.sh is not executable",
            "verifier bootstrap",
            "pytest import",
            "ctrf",
            "exceptiongroup",
            "tomli",
            "uvx: command not found",
            "environment_not_ready",
        )
    )


def _docker_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "available": False,
        "containers": {},
        "volumes": [],
        "images": [],
    }
    docker = shutil.which("docker")
    if docker is None:
        return snapshot
    containers = subprocess.run(
        [docker, "ps", "-a", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if containers.returncode != 0:
        snapshot["error"] = containers.stderr.strip()[-2_000:]
        return snapshot
    records: dict[str, Any] = {}
    for line in containers.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier = str(item.get("ID") or "")
        if identifier:
            records[identifier] = item
    snapshot["available"] = True
    snapshot["containers"] = records
    for key, command in (
        ("volumes", [docker, "volume", "ls", "-q"]),
        ("images", [docker, "image", "ls", "-q", "--no-trunc"]),
    ):
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0:
            snapshot[key] = sorted(set(completed.stdout.split()))
    return snapshot


def _harbor_host_import_check(project_root: Path) -> tuple[bool, str | None]:
    """Verify Harbor can import the host-side custom agent before Docker work."""

    uvx = shutil.which("uvx")
    if uvx is None:
        return False, "uvx is unavailable for the Harbor host import check"
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join(
            value for value in (str(project_root), os.environ.get("PYTHONPATH")) if value
        ),
    }
    command = [
        uvx,
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "python",
        "-c",
        "import harbor_plugins.cc_harness_agent",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_HARBOR_HOST_IMPORT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Harbor host agent import check failed: {exc}"
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        return False, f"Harbor host agent import failed: {diagnostic[-2_000:]}"
    return True, None


def _docker_healthcheck(
    *,
    attempts: int = _DOCKER_HEALTHCHECK_ATTEMPTS,
    backoffs: Sequence[float] = _DOCKER_HEALTHCHECK_BACKOFFS,
) -> dict[str, Any]:
    """Probe Docker with a short bounded retry window.

    This check is intentionally read-only.  It is used at formal preflight
    and immediately before Harbor starts a task because Docker Desktop can be
    restarting between those two moments.  A failed probe is retained as
    evidence so the runner can retry without spending model calls.
    """

    docker = shutil.which("docker")
    result: dict[str, Any] = {
        "ready": False,
        "attempts": 0,
        "timeout_seconds": _DOCKER_HEALTHCHECK_TIMEOUT_SECONDS,
        "backoff_seconds": list(backoffs),
        "last_error": None,
    }
    if docker is None:
        result["last_error"] = "docker executable is unavailable"
        return result
    bounded_attempts = max(1, int(attempts))
    for attempt in range(1, bounded_attempts + 1):
        result["attempts"] = attempt
        try:
            completed = subprocess.run(
                [docker, "info", "--format", "{{.DockerRootDir}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_DOCKER_HEALTHCHECK_TIMEOUT_SECONDS,
            )
            diagnostic = (completed.stderr or completed.stdout or "").strip()
            if completed.returncode == 0:
                result["ready"] = True
                result["docker_root"] = (completed.stdout or "").strip()
                result["last_error"] = None
                return result
            result["last_error"] = diagnostic or f"docker info exited {completed.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["last_error"] = f"{type(exc).__name__}: {exc}"
        if attempt < bounded_attempts:
            delay = float(backoffs[min(attempt - 1, len(backoffs) - 1)]) if backoffs else 0.0
            if delay > 0:
                time.sleep(delay)
    return result


def _cleanup_owned_harbor_resources(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove only newly-created Harbor-owned containers after a trial ends.

    Harbor normally removes its environment, but an exception can happen after
    the temporary compose file has been deleted.  In that case a running
    task-owned container would otherwise survive the attempt.  The before/after
    identity fence prevents touching containers that predate this attempt; a
    running owned container is stopped gracefully before normal removal.
    """

    result: dict[str, Any] = {
        "policy": "new-and-harbor-labeled-containers-after-attempt",
        "stopped_containers": [],
        "forced_removed_containers": [],
        "removed_containers": [],
        "retained_candidates": [],
        "errors": [],
    }
    if not before.get("available") or not after.get("available"):
        result["skipped"] = "docker snapshot unavailable"
        return result
    docker = shutil.which("docker")
    if docker is None:
        result["skipped"] = "docker executable unavailable"
        return result
    before_ids = set((before.get("containers") or {}).keys())
    for identifier, item in (after.get("containers") or {}).items():
        if identifier in before_ids:
            continue
        labels = str(item.get("Labels") or "").casefold()
        status = str(item.get("State") or item.get("Status") or "").casefold()
        owned = "harbor" in labels
        stopped = not any(word in status for word in ("running", "restarting", "paused"))
        if not owned:
            result["retained_candidates"].append(
                {"id": identifier, "owned": owned, "status": status}
            )
            continue
        if not stopped:
            stopped_result = subprocess.run(
                [docker, "stop", "--time", "10", identifier],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if stopped_result.returncode == 0:
                result["stopped_containers"].append(identifier)
            else:
                forced = subprocess.run(
                    [docker, "rm", "-f", identifier],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if forced.returncode == 0:
                    result["forced_removed_containers"].append(identifier)
                    continue
                result["errors"].append(
                    {
                        "id": identifier,
                        "phase": "stop/remove",
                        "error": (
                            stopped_result.stderr.strip()[-500:]
                            or forced.stderr.strip()[-500:]
                        ),
                    }
                )
                continue
        completed = subprocess.run(
            [docker, "rm", identifier],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0:
            result["removed_containers"].append(identifier)
        else:
            result["errors"].append(
                {"id": identifier, "error": completed.stderr.strip()[-1_000:]}
            )
    # Image and volume candidates are intentionally retained unless Harbor
    # exposes a task/run ownership label for them. Shared layers remain useful,
    # and set-difference alone is not sufficient proof of ownership.
    result["new_image_candidates"] = sorted(
        set(after.get("images") or ()) - set(before.get("images") or ())
    )
    result["new_volume_candidates"] = sorted(
        set(after.get("volumes") or ()) - set(before.get("volumes") or ())
    )
    return result


def _docker_storage_path() -> Path | None:
    if os.name != "nt":
        root = Path("/var/lib/docker")
        return root if root.exists() else None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    for name in ("settings-store.json", "settings.json"):
        settings = Path(appdata) / "Docker" / name
        if not settings.is_file():
            continue
        try:
            payload = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        configured = payload.get("CustomWslDistroDir") or payload.get("diskImageLocation")
        if isinstance(configured, str) and Path(configured).exists():
            return Path(configured)
    return None
