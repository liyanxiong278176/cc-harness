"""Resumable, block-randomized live Harbor parity execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.core import canonical_json_bytes
from eval.launch import PARITY_MODEL, HarnessKind
from eval.parity import ParitySuite, analyze_imported_parity, build_balanced_schedule

from .export import export_harbor_jobs

HARBOR_VERSION = "0.20.0"
CLAUDE_CODE_VERSION = "2.1.221"
SWEBENCH_DATASET = (
    "swe-bench/swe-bench-verified@"
    "sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341"
)
Progress = Callable[[str], None]


def run_harbor_parity(
    project_root: Path,
    output_root: Path,
    *,
    task_names: tuple[str, ...],
    wheel_path: Path,
    env_file: Path,
    claude_settings_path: Path,
    repetitions: int,
    random_seed: int,
    maximum_attempts: int,
    cooldown_seconds: float,
    suite: ParitySuite = ParitySuite.DEV,
    progress: Progress | None = None,
    task_catalog_path: Path | None = None,
) -> Path:
    """Run or resume paired Harbor trials and return the normalized bundle path."""
    if maximum_attempts <= 0:
        raise ValueError("maximum_attempts must be positive")
    if cooldown_seconds < 0:
        raise ValueError("cooldown_seconds cannot be negative")
    root = project_root.resolve()
    evidence_root = output_root.resolve()
    wheel = wheel_path.resolve()
    dotenv = env_file.resolve()
    settings_path = claude_settings_path.resolve()
    task_catalog = task_catalog_path.resolve() if task_catalog_path is not None else None
    for path, label in (
        (wheel, "cc-harness wheel"),
        (dotenv, "environment file"),
        (settings_path, "Claude settings"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} is missing: {path}")
    if task_catalog is not None and not task_catalog.is_file():
        raise ValueError(f"task catalog is missing: {task_catalog}")
    if not task_names or len(set(task_names)) != len(task_names):
        raise ValueError("task names must be non-empty and unique")

    schedule = build_balanced_schedule(task_names, repetitions=repetitions, random_seed=random_seed)
    wheel_digest = _file_digest(wheel)
    frozen_wheel = evidence_root / "frozen-inputs" / wheel.name
    config = {
        "schema_version": "eval.harbor-paired-config.v1",
        "harbor_version": HARBOR_VERSION,
        "claude_code_version": CLAUDE_CODE_VERSION,
        "model": PARITY_MODEL,
        "dataset": SWEBENCH_DATASET,
        "task_names": list(task_names),
        "repetitions": repetitions,
        "random_seed": random_seed,
        "maximum_attempts": maximum_attempts,
        "cooldown_seconds": cooldown_seconds,
        "wheel_path": str(wheel),
        "wheel_digest": wheel_digest,
        "frozen_wheel_path": str(frozen_wheel),
        "env_file_digest": _file_digest(dotenv),
        "claude_settings_digest": _file_digest(settings_path),
        "env_file": str(dotenv),
        "claude_settings_path": str(settings_path),
    }
    frozen_catalog: Path | None = None
    if task_catalog is not None:
        frozen_catalog = evidence_root / "frozen-inputs" / task_catalog.name
        config.update(
            {
                "task_catalog_path": str(task_catalog),
                "task_catalog_digest": _file_digest(task_catalog),
                "frozen_task_catalog_path": str(frozen_catalog),
            }
        )
    state_path = evidence_root / "state.json"
    schedule_path = evidence_root / "schedule.json"
    state = _load_or_initialize_state(evidence_root, state_path, schedule_path, config, schedule)
    if frozen_wheel.is_file():
        if _file_digest(frozen_wheel) != wheel_digest:
            raise ValueError("frozen cc-harness wheel does not match the run contract")
    else:
        frozen_wheel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wheel, frozen_wheel)
    if task_catalog is not None and frozen_catalog is not None:
        catalog_digest = _file_digest(task_catalog)
        if frozen_catalog.is_file():
            if _file_digest(frozen_catalog) != catalog_digest:
                raise ValueError("frozen task catalog does not match the run contract")
        else:
            frozen_catalog.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(task_catalog, frozen_catalog)
    launch_env = _launch_environment(root, settings_path)
    uvx = shutil.which("uvx")
    if uvx is None:
        raise ValueError("uvx is required to run Harbor parity")
    callback = progress or (lambda _message: None)
    progress_path = evidence_root / "progress.log"

    def emit(message: str) -> None:
        stamp = datetime.now(UTC).isoformat()
        with progress_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{stamp} {message}\n")
        callback(message)

    for pair in schedule.pairs:
        for harness in pair.order:
            key = f"{pair.sequence}.{harness.value}"
            job_state = state["jobs"].get(key) or {}
            selected = job_state.get("selected_job")
            if selected and (Path(selected) / "result.json").is_file():
                if not _selected_job_needs_retry(job_state, maximum_attempts):
                    emit(f"skip completed {key}: {selected}")
                    continue
                emit(
                    f"resume transient {key}: selected={selected} "
                    f"next_attempt={len(job_state.get('attempts') or []) + 1}"
                )
            task_slug = _slug(pair.task_id)
            recorded_attempts: list[dict[str, Any]] = list(job_state.get("attempts") or [])
            attempts = [item for item in recorded_attempts if item.get("job_root")]
            launcher_failures: list[dict[str, Any]] = list(job_state.get("launcher_failures") or [])
            legacy_launcher_failures = [
                item for item in recorded_attempts if not item.get("job_root")
            ]
            if legacy_launcher_failures:
                launcher_failures.extend(legacy_launcher_failures)
                emit(f"migrate launcher failures {key}: count={len(legacy_launcher_failures)}")
            selected_job = (
                Path(selected) if selected and (Path(selected) / "result.json").is_file() else None
            )
            launcher_failures_this_run = 0
            launcher_exhausted = False
            while len(attempts) < maximum_attempts:
                attempt = len(attempts) + 1
                jobs_dir = (
                    evidence_root
                    / "raw"
                    / f"{pair.sequence:04d}-{task_slug}"
                    / f"r{pair.repetition}"
                    / harness.value
                    / f"attempt-{attempt}"
                )
                interrupted = _prepare_attempt_jobs_dir(jobs_dir)
                if interrupted is not None:
                    emit(f"archive interrupted {key} attempt={attempt}: {interrupted}")
                command = build_harbor_command(
                    uvx=uvx,
                    project_root=root,
                    dataset=SWEBENCH_DATASET,
                    task_name=pair.task_id,
                    harness=harness,
                    wheel_path=frozen_wheel,
                    env_file=dotenv,
                    jobs_dir=jobs_dir,
                )
                emit(
                    f"run pair={pair.sequence} task={pair.task_id} "
                    f"rep={pair.repetition} harness={harness.value} attempt={attempt}"
                )
                state["completed_at"] = None
                _write_json(state_path, state)
                started = datetime.now(UTC)
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env=launch_env,
                    check=False,
                )
                job_root = _single_job_root(jobs_dir)
                exception = _job_exception(job_root) if job_root is not None else None
                transient = (
                    _job_exception_is_transient(job_root)
                    if job_root is not None and exception is not None
                    else job_root is None
                )
                attempt_record = {
                    "attempt": attempt,
                    "started_at": started.isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "return_code": completed.returncode,
                    "job_root": str(job_root) if job_root is not None else None,
                    "exception_type": exception,
                    "transient": transient,
                }
                if job_root is None:
                    launcher_failures_this_run += 1
                    launcher_failures.append(attempt_record)
                    state["jobs"][key] = {
                        "pair_sequence": pair.sequence,
                        "task_id": pair.task_id,
                        "repetition": pair.repetition,
                        "harness": harness.value,
                        "attempts": attempts,
                        "launcher_failures": launcher_failures,
                        "selected_job": str(selected_job) if selected_job is not None else None,
                    }
                    _write_json(state_path, state)
                    if launcher_failures_this_run < maximum_attempts:
                        emit(f"retry launcher {key} after return_code={completed.returncode}")
                        time.sleep(cooldown_seconds)
                        continue
                    launcher_exhausted = True
                    break
                attempts.append(attempt_record)
                selected_job = job_root
                state["jobs"][key] = {
                    "pair_sequence": pair.sequence,
                    "task_id": pair.task_id,
                    "repetition": pair.repetition,
                    "harness": harness.value,
                    "attempts": attempts,
                    "launcher_failures": launcher_failures,
                    "selected_job": None,
                }
                state["normalized_dirty"] = True
                state["analysis_dirty"] = True
                state["bundle_path"] = None
                state["analysis_root"] = None
                _write_json(state_path, state)
                if completed.returncode == 0 and job_root is not None and exception is None:
                    break
                if len(attempts) < maximum_attempts and transient:
                    emit(f"retry {key} after exception={exception}")
                    time.sleep(cooldown_seconds)
                    continue
                break
            if launcher_exhausted:
                raise RuntimeError(
                    f"Harbor launcher failed before producing an auditable job for {key}; "
                    "fix the local runtime and rerun the same command"
                )
            if selected_job is None:
                raise RuntimeError(f"Harbor produced no auditable job for {key}")
            state["jobs"][key]["selected_job"] = str(selected_job)
            _write_json(state_path, state)

    candidate_jobs, baseline_jobs = _selected_jobs(state, schedule)
    normalized_root = evidence_root / "normalized"
    bundle_path = normalized_root / "bundle.json"
    if state.get("normalized_dirty", False) or not bundle_path.is_file():
        bundle_path = _materialize_normalized_bundle(
            candidate_jobs, baseline_jobs, normalized_root, schedule=schedule
        )
        state["normalized_dirty"] = False
        state["bundle_path"] = str(bundle_path)
        _write_json(state_path, state)
    analysis_root = evidence_root / "analysis"
    if state.get("analysis_dirty", False) or not (analysis_root / "summary.json").is_file():
        _materialize_analysis(bundle_path, analysis_root, suite=suite)
        state["analysis_dirty"] = False
        state["analysis_root"] = str(analysis_root)
        _write_json(state_path, state)
    state["completed_at"] = datetime.now(UTC).isoformat()
    state["bundle_path"] = str(bundle_path)
    state["analysis_root"] = str(analysis_root)
    _write_json(state_path, state)
    return bundle_path


def _materialize_normalized_bundle(
    candidate_jobs: tuple[Path, ...],
    baseline_jobs: tuple[Path, ...],
    normalized_root: Path,
    *,
    schedule: Any | None = None,
) -> Path:
    parent = normalized_root.parent
    staging = parent / f".{normalized_root.name}-staging-{uuid.uuid4().hex}"
    try:
        export_harbor_jobs(
            candidate_jobs,
            baseline_jobs,
            staging,
            **({"schedule": schedule} if schedule is not None else {}),
        )
    except Exception:
        if staging.exists():
            failed = parent / (
                f"{normalized_root.name}-failed-"
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            )
            staging.replace(failed)
        raise
    if normalized_root.exists():
        disposition = "superseded" if (normalized_root / "bundle.json").is_file() else "failed"
        normalized_root.replace(_archive_path(normalized_root, disposition))
    staging.replace(normalized_root)
    return normalized_root / "bundle.json"


def _materialize_analysis(
    bundle_path: Path,
    analysis_root: Path,
    *,
    suite: ParitySuite,
) -> None:
    parent = analysis_root.parent
    staging = parent / f".{analysis_root.name}-staging-{uuid.uuid4().hex}"
    try:
        analyze_imported_parity((bundle_path,), staging, suite=suite)
    except Exception:
        if staging.exists():
            staging.replace(_archive_path(analysis_root, "failed"))
        raise
    if analysis_root.exists():
        disposition = "superseded" if (analysis_root / "summary.json").is_file() else "failed"
        analysis_root.replace(_archive_path(analysis_root, disposition))
    staging.replace(analysis_root)


def _archive_path(root: Path, disposition: str) -> Path:
    return root.parent / (
        f"{root.name}-{disposition}-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )


def _prepare_attempt_jobs_dir(jobs_dir: Path) -> Path | None:
    archived: Path | None = None
    if jobs_dir.exists() and any(jobs_dir.iterdir()):
        archived = _archive_path(jobs_dir, "interrupted")
        jobs_dir.replace(archived)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    return archived


def build_harbor_command(
    *,
    uvx: str,
    project_root: Path,
    dataset: str,
    task_name: str,
    harness: HarnessKind,
    wheel_path: Path,
    env_file: Path,
    jobs_dir: Path,
    uv_bootstrap_path: Path | None = None,
    verifier_bootstrap_path: Path | None = None,
    tiktoken_bootstrap_path: Path | None = None,
    install_only: bool = False,
    delete: bool | None = None,
    force_build: bool | None = None,
    timeout_multiplier: float | None = None,
    agent_timeout_multiplier: float | None = None,
    agent_setup_timeout_multiplier: float | None = None,
    environment_build_timeout_multiplier: float | None = None,
    verifier_timeout_multiplier: float | None = None,
    agent_env: dict[str, str] | None = None,
    extra_docker_compose_paths: Sequence[Path] | None = None,
    allowed_agent_hosts: Sequence[str] | None = None,
) -> list[str]:
    command = [
        uvx,
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "harbor",
        "run",
        "--dataset",
        dataset,
        "--include-task-name",
        task_name,
    ]
    if harness is HarnessKind.CC_HARNESS:
        command.extend(
            [
                "--agent",
                "harbor_plugins.cc_harness_agent:CCHarnessHarborAgent",
                "--agent-kwarg",
                f"wheel_path={wheel_path}",
            ]
        )
        if uv_bootstrap_path is not None:
            command.extend(["--agent-kwarg", f"uv_bootstrap_path={uv_bootstrap_path}"])
        if verifier_bootstrap_path is not None:
            command.extend(
                ["--agent-kwarg", f"verifier_bootstrap_path={verifier_bootstrap_path}"]
            )
        if tiktoken_bootstrap_path is not None:
            command.extend(
                ["--agent-kwarg", f"tiktoken_bootstrap_path={tiktoken_bootstrap_path}"]
            )
    elif harness is HarnessKind.CLAUDE_CODE:
        command.extend(
            ["--agent", "claude-code", "--agent-kwarg", f"version={CLAUDE_CODE_VERSION}"]
        )
    else:
        raise ValueError(f"unsupported Harbor parity harness: {harness}")
    command.extend(
        [
            "--model",
            PARITY_MODEL,
            "--env-file",
            str(env_file),
            "--n-attempts",
            "1",
            "--n-concurrent",
            "1",
            "--jobs-dir",
            str(jobs_dir),
            "--quiet",
            "--yes",
        ]
    )
    for option, value in (
        ("--timeout-multiplier", timeout_multiplier),
        ("--agent-timeout-multiplier", agent_timeout_multiplier),
        ("--agent-setup-timeout-multiplier", agent_setup_timeout_multiplier),
        ("--environment-build-timeout-multiplier", environment_build_timeout_multiplier),
        ("--verifier-timeout-multiplier", verifier_timeout_multiplier),
    ):
        if value is None:
            continue
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{option} must be a positive number")
        command.extend([option, str(value)])
    for key, value in sorted((agent_env or {}).items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid Harbor agent environment name: {key}")
        if not isinstance(value, str):
            raise TypeError(f"Harbor agent environment value must be text: {key}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"Harbor agent environment value contains a newline: {key}")
        command.extend(["--agent-env", f"{key}={value}"])
    for path in extra_docker_compose_paths or ():
        command.extend(["--extra-docker-compose", str(path)])
    for host in allowed_agent_hosts or ():
        if not host or "\n" in host or "\r" in host:
            raise ValueError("invalid Harbor allowed agent host")
        command.extend(["--allow-agent-host", host])
    if install_only:
        command.append("--install-only")
    if delete is not None:
        command.append("--delete" if delete else "--no-delete")
    if force_build is not None:
        command.append("--force-build" if force_build else "--no-force-build")
    return command


def _load_or_initialize_state(
    root: Path,
    state_path: Path,
    schedule_path: Path,
    config: dict[str, Any],
    schedule: Any,
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    if state_path.is_file():
        state = _read_json(state_path)
        if state.get("config") != config:
            raise ValueError("existing Harbor parity state does not match requested config")
        persisted_schedule = _read_json(schedule_path)
        if persisted_schedule != schedule.model_dump(mode="json"):
            raise ValueError("existing Harbor parity schedule does not match requested schedule")
        return state
    unexpected = [path for path in root.iterdir() if path not in {state_path, schedule_path}]
    if unexpected:
        raise ValueError(f"new Harbor parity output root is not empty: {root}")
    _write_json(schedule_path, schedule.model_dump(mode="json"))
    state = {
        "schema_version": "eval.harbor-paired-state.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "config": config,
        "jobs": {},
        "completed_at": None,
        "bundle_path": None,
        "analysis_root": None,
        "normalized_dirty": False,
        "analysis_dirty": False,
    }
    _write_json(state_path, state)
    return state


def _launch_environment(project_root: Path, settings_path: Path) -> dict[str, str]:
    settings = _read_json(settings_path)
    settings_env = settings.get("env") or {}
    if not isinstance(settings_env, dict):
        raise TypeError("Claude settings env must be an object")
    token = settings_env.get("ANTHROPIC_API_KEY") or settings_env.get("ANTHROPIC_AUTH_TOKEN")
    base_url = settings_env.get("ANTHROPIC_BASE_URL")
    if not isinstance(token, str) or not token or not isinstance(base_url, str) or not base_url:
        raise ValueError("Claude settings lack Anthropic-compatible route credentials")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(project_root)
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_BASE_URL"] = base_url
    return env


def _single_job_root(jobs_dir: Path) -> Path | None:
    roots = sorted(
        (path for path in jobs_dir.iterdir() if (path / "result.json").is_file()),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if len(roots) > 1:
        raise RuntimeError(f"Harbor attempt produced multiple job roots: {jobs_dir}")
    return roots[0] if roots else None


def _job_exception(job_root: Path) -> str | None:
    exception = _job_exception_document(job_root)
    if exception is None:
        return None
    return str(exception.get("exception_type") or "HarborException")


def _job_exception_document(job_root: Path) -> dict[str, Any] | None:
    trial_results = [
        _read_json(path / "result.json")
        for path in job_root.iterdir()
        if path.is_dir() and (path / "result.json").is_file()
    ]
    if len(trial_results) != 1:
        raise RuntimeError(f"paired Harbor job must contain exactly one trial: {job_root}")
    exception = trial_results[0].get("exception_info")
    if exception is None:
        return None
    if not isinstance(exception, dict):
        return {"exception_type": "UnsupportedException"}
    return exception


def _job_exception_is_transient(job_root: Path) -> bool:
    exception = _job_exception_document(job_root)
    if exception is None:
        return False
    exception_type = str(exception.get("exception_type") or "").lower()
    message = re.sub(r"\s+", " ", _job_diagnostic_text(job_root, exception).lower())
    if exception_type in {
        "agentsetuptimeouterror",
        "apiusagelimiterror",
        "environmentbuildtimeouterror",
        "ratelimiterror",
        "networkerror",
        "serviceunavailableerror",
    }:
        return True
    markers = (
        "apiconnectionerror",
        "connection error",
        "connection reset",
        "rate limit",
        "temporarily unavailable",
        "service unavailable",
        "http 429",
        "http 502",
        "http 503",
        "http 504",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
    )
    if any(marker in message for marker in markers):
        return True
    docker_markers = (
        "421 misdirected request",
        "short read",
        "unexpected eof",
        "unexpected commit digest",
        "tls handshake timeout",
        "i/o timeout",
        "temporary failure in name resolution",
    )
    is_docker_setup_failure = (
        "docker compose command failed for environment" in message or "failed to solve" in message
    )
    return is_docker_setup_failure and any(marker in message for marker in docker_markers)


def _job_diagnostic_text(job_root: Path, exception: dict[str, Any]) -> str:
    parts = [str(exception.get("exception_message") or "")]
    for trial_root in sorted(path for path in job_root.iterdir() if path.is_dir()):
        for name in ("trial.log", "exception.txt"):
            path = trial_root / name
            if path.is_file():
                parts.append(_read_text_tail(path, maximum_bytes=256 * 1024))
    return "\n".join(parts)


def _read_text_tail(path: Path, *, maximum_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - maximum_bytes))
        return handle.read(maximum_bytes).decode("utf-8", errors="replace")


def _selected_job_needs_retry(job_state: dict[str, Any], maximum_attempts: int) -> bool:
    attempts = job_state.get("attempts") or []
    selected = job_state.get("selected_job")
    if len(attempts) >= maximum_attempts or not isinstance(selected, str):
        return False
    selected_root = Path(selected)
    if not (selected_root / "result.json").is_file():
        return False
    return _job_exception(selected_root) is not None and _job_exception_is_transient(selected_root)


def _selected_jobs(
    state: dict[str, Any], schedule: Any
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    selected: dict[HarnessKind, list[Path]] = {
        HarnessKind.CC_HARNESS: [],
        HarnessKind.CLAUDE_CODE: [],
    }
    for pair in schedule.pairs:
        for harness in pair.order:
            key = f"{pair.sequence}.{harness.value}"
            path = (state["jobs"].get(key) or {}).get("selected_job")
            if not isinstance(path, str) or not (Path(path) / "result.json").is_file():
                raise RuntimeError(f"Harbor parity state lacks selected job for {key}")
            selected[harness].append(Path(path))
    return tuple(selected[HarnessKind.CC_HARNESS]), tuple(selected[HarnessKind.CLAUDE_CODE])


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"JSON document must be an object: {path}")
    return document


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(document))
    temporary.replace(path)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")[:80]


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
