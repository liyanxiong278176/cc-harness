"""Prepare Terminal-Bench Docker images without making model calls.

Harbor creates a fresh task container for each formal trial.  Its Docker image
name is content-addressed by the task environment definition, so a separate
install-only run can build the image once and a later scoring run can reuse the
Docker image/build cache while still receiving an isolated container.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.cc_only.contracts import BenchmarkTask, EvalProfile, TrialStatus
from eval.cc_only.storage import (
    RunStateStore,
    atomic_json,
    atomic_text,
    digest_file,
    utc_now,
)
from eval.harbor.paired import HARBOR_VERSION, build_harbor_command
from eval.launch import HarnessKind

from .adapters.harbor import TERMINAL_BENCH_21_DATASET, TerminalBenchAdapter
from .terminal_bootstrap import ensure_uv_bootstrap, uv_bootstrap_cache_path
from .terminal_host import inspect_terminal_host
from .tiktoken_bootstrap import (
    ensure_tiktoken_bootstrap,
    tiktoken_bootstrap_cache_path,
    tiktoken_bootstrap_identity,
)
from .verifier_bootstrap import (
    ensure_verifier_bootstrap,
    verifier_bootstrap_cache_path,
    verifier_bootstrap_identity,
)
from .verifier_runtime import (
    ensure_verifier_runtime,
    verifier_runtime_cache_path,
    verifier_runtime_identity,
    verifier_runtime_overlay,
)

PREWARM_SCHEMA = "terminal-bench.docker-prewarm.v6"
PREWARM_ROOT_NAME = "docker-prewarm"
HEARTBEAT_SECONDS = 15
VERIFIER_SMOKE_TIMEOUT_S = 300
# Keep verifier smoke's infrastructure budgets aligned with the formal
# Terminal-Bench adapter.  Heavy video/data tasks can spend several minutes
# unpacking dependencies before the no-model test script starts; the default
# Harbor 360s setup budget otherwise misclassifies that work as infrastructure
# failure.
PREWARM_TIMEOUT_MULTIPLIER = 2.0
PREWARM_AGENT_TIMEOUT_MULTIPLIER = 2.0
PREWARM_AGENT_SETUP_TIMEOUT_MULTIPLIER = 4.0
PREWARM_ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER = 4.0
PREWARM_VERIFIER_TIMEOUT_MULTIPLIER = 2.0

Progress = Callable[[str], None]


def terminal_plugin_digest(project_root: Path) -> str:
    """Digest host-side Harbor plugin sources used by formal trials."""

    plugin_root = project_root / "harbor_plugins"
    paths = sorted(plugin_root.glob("*.py"))
    if not paths:
        raise FileNotFoundError(f"Harbor plugin sources are missing: {plugin_root}")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def run_terminal_bench_prewarm(
    project_root: Path,
    output_root: Path,
    *,
    task_limit: int | None = None,
    task_ids: Sequence[str] | None = None,
    maximum_attempts: int = 3,
    force_build: bool = False,
    retry_failed: bool = False,
    progress: Progress | None = None,
) -> dict[str, Path]:
    """Validate all selected task environments without making model calls.

    The prewarm performs the same frozen/offline agent installation as a live
    trial, but the agent turn itself is a no-op, followed by the official
    verifier in a fresh container.  It is deliberately not Harbor's
    ``--install-only`` mode: that mode skips the agent setup path and cannot
    catch missing Python/loader dependencies before a paid run.
    """
    root = project_root.resolve()
    evidence_root = output_root.resolve()
    if maximum_attempts <= 0:
        raise ValueError("maximum_attempts must be positive")
    if task_limit is not None and task_limit <= 0:
        raise ValueError("task_limit must be positive")
    if task_limit is not None and task_ids:
        raise ValueError("task_limit cannot be combined with exact task selection")

    adapter = TerminalBenchAdapter()
    all_tasks = tuple(adapter.catalog(root, EvalProfile.FULL))
    if task_limit is None and not task_ids and len(all_tasks) != 89:
        raise ValueError(
            f"Terminal-Bench 2.1 catalog must contain 89 tasks, got {len(all_tasks)}"
        )
    selected_task_ids = tuple(dict.fromkeys(task_ids or ()))
    if selected_task_ids:
        catalog_by_id = {task.task_id: task for task in all_tasks}
        missing = [task_id for task_id in selected_task_ids if task_id not in catalog_by_id]
        if missing:
            raise ValueError(
                "task selection contains ids outside the frozen catalog: "
                + ", ".join(missing[:5])
            )
        tasks = tuple(catalog_by_id[task_id] for task_id in selected_task_ids)
    else:
        tasks = all_tasks[:task_limit] if task_limit is not None else all_tasks
    if not tasks:
        raise ValueError("exact task selection cannot be empty")
    if task_limit is None and not selected_task_ids and len(tasks) != 89:
        raise ValueError(f"Terminal-Bench 2.1 catalog must contain 89 tasks, got {len(tasks)}")
    # FULL readiness validates the pinned 89-task dataset contract. Exact
    # selections are applied only to the expensive container/verifier smoke.
    # Passing 30 selected tasks into FULL check would correctly fail its
    # official-catalog cardinality assertion.
    checked = adapter.check(
        root,
        EvalProfile.CHECK if task_limit is not None else EvalProfile.FULL,
        all_tasks if selected_task_ids else tasks,
    )
    if not checked.ready:
        raise RuntimeError(
            "Docker prewarm prerequisites are not ready: "
            + "; ".join(checked.warnings or (str(checked.details),))
        )

    # Prepare all host-side immutable artifacts before creating the prewarm
    # contract.  A fresh checkout must not fail merely because the cache has
    # not been generated yet; the network is touched once here, never per
    # trial/container.
    ensure_uv_bootstrap(root, progress=progress)
    verifier_bootstrap = ensure_verifier_bootstrap(root, progress=progress)
    verifier_runtime = ensure_verifier_runtime(root, progress=progress)
    ensure_tiktoken_bootstrap(root, progress=progress)
    wheel = adapter._wheel(root)
    wheel_digest = digest_file(wheel)
    execution_backend = inspect_terminal_host(root)
    contract: dict[str, Any] = {
        "benchmark": adapter.slug,
        "benchmark_title": adapter.title,
        "protocol_version": PREWARM_SCHEMA,
        "profile": EvalProfile.FULL.value,
        "system": "cc-harness",
        "model": None,
        "dataset": TERMINAL_BENCH_21_DATASET,
        "harbor_version": HARBOR_VERSION,
        "wheel_sha256": wheel_digest,
        "harbor_plugin_sha256": terminal_plugin_digest(root),
        "execution_backend": execution_backend,
        "offline_verifier_bootstrap": verifier_bootstrap_identity(
            verifier_bootstrap_cache_path(root)
        ),
        "offline_verifier_runtime": verifier_runtime_identity(verifier_runtime),
        "offline_tiktoken_bootstrap": tiktoken_bootstrap_identity(
            tiktoken_bootstrap_cache_path(root)
        ),
        "prewarm": {
            "install_only": False,
            "delete": True,
            "force_build": force_build,
            "model_calls": 0,
            "verifier_calls": 0,
            "verifier_smoke": {
                "enabled": True,
                "scored": False,
                "agent_mode": "no-model",
                "agent_install": "full-frozen-offline",
                "timeout_seconds": VERIFIER_SMOKE_TIMEOUT_S,
            },
            "reuse": "content-addressed Docker image/build cache",
        },
        "task_limit": task_limit,
        "task_count": len(tasks),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cc_harness_version": _package_version(),
        },
    }
    if selected_task_ids:
        contract["task_selection"] = list(selected_task_ids)
        contract["task_selection_digest"] = (
            "sha256:"
            + hashlib.sha256("\n".join(selected_task_ids).encode("utf-8")).hexdigest()
        )
    store = RunStateStore(evidence_root)
    state = store.initialize(contract=contract, tasks=tasks)
    verifier_overlay = verifier_runtime_overlay(
        root, evidence_root, runtime_path=verifier_runtime
    )
    atomic_json(evidence_root / "check.json", checked.as_dict())
    adapter.freeze_inputs(root, evidence_root)
    _freeze_prewarm_artifact(
        verifier_bootstrap,
        evidence_root / "frozen-inputs" / verifier_bootstrap.name,
    )
    progress_path = evidence_root / "progress.jsonl"
    progress_path.touch(exist_ok=True)
    emit = _progress_writer(progress_path, progress)
    state["preflight"] = {
        "status": TrialStatus.PASS.value,
        "model_calls": 0,
        "checked_at": utc_now(),
        "details": checked.as_dict(),
    }
    store.save(state)

    uvx = shutil.which("uvx")
    if uvx is None:
        raise RuntimeError("uvx is required to run Harbor")
    env_file = root / ".env"
    for index, task in enumerate(tasks, start=1):
        trial = state["trials"][task.task_id]
        if trial.get("status") == TrialStatus.PASS.value:
            emit(f"skip prepared {index}/{len(tasks)} {task.task_id}")
            continue
        allow_failed_retry = retry_failed
        if trial.get("status") == TrialStatus.FAIL.value and not retry_failed:
            previous_class = _last_prewarm_failure_class(evidence_root, trial)
            if previous_class == "environment_not_ready":
                emit(
                    f"skip deterministic failure {index}/{len(tasks)} {task.task_id} "
                    "(repair the environment or use --retry-failed)"
                )
                continue
            # A transient Harbor/Docker failure is safe to retry on the next
            # invocation of the same immutable command.  This is still a
            # no-model install-only attempt and never replays a scored task.
            allow_failed_retry = True
        if allow_failed_retry and trial.get("status") == TrialStatus.FAIL.value:
            trial["status"] = "pending"
            store.save(state)
        attempt_budget = maximum_attempts
        if allow_failed_retry:
            # Keep prior failed evidence, but grant this invocation one fresh
            # attempt even when the previous budget was exhausted.
            attempt_budget = max(attempt_budget, len(trial.get("attempts") or []) + 1)
        while trial.get("status") != TrialStatus.PASS.value:
            if len(trial.get("attempts") or []) >= attempt_budget:
                emit(
                    f"skip exhausted {index}/{len(tasks)} {task.task_id} "
                    f"attempts={len(trial.get('attempts') or [])}"
                )
                break
            attempt, attempt_root, record = store.begin_attempt(
                state, task, index, reuse_interrupted=True
            )
            evidence_jobs = attempt_root / "jobs"
            runtime_root = os.environ.get("CC_HARNESS_TERMINAL_RUNTIME_ROOT")
            if not runtime_root:
                raise RuntimeError(
                    "Terminal-Bench prewarm requires CC_HARNESS_TERMINAL_RUNTIME_ROOT"
                )
            run_identity = hashlib.sha256(
                str(evidence_root).encode("utf-8")
            ).hexdigest()[:16]
            task_identity = hashlib.sha256(task.task_id.encode("utf-8")).hexdigest()[:12]
            jobs_dir = (
                Path(runtime_root)
                / "prewarm"
                / run_identity
                / f"{index:04d}-{task_identity}"
                / f"attempt-{attempt}"
                / "jobs"
            )
            jobs_dir.mkdir(parents=True, exist_ok=True)
            command = build_harbor_command(
                uvx=uvx,
                project_root=root,
                dataset=TERMINAL_BENCH_21_DATASET,
                task_name=str(task.payload["harbor_task_name"]),
                harness=HarnessKind.CC_HARNESS,
                wheel_path=evidence_root / "frozen-inputs" / wheel.name,
                uv_bootstrap_path=(
                    evidence_root / "frozen-inputs" / uv_bootstrap_cache_path(root).name
                ),
                verifier_bootstrap_path=(
                    evidence_root
                    / "frozen-inputs"
                    / verifier_bootstrap_cache_path(root).name
                ),
                tiktoken_bootstrap_path=(
                    evidence_root
                    / "frozen-inputs"
                    / tiktoken_bootstrap_cache_path(root).name
                ),
                env_file=env_file,
                jobs_dir=jobs_dir,
                install_only=False,
                delete=True,
                force_build=force_build,
                timeout_multiplier=PREWARM_TIMEOUT_MULTIPLIER,
                agent_timeout_multiplier=PREWARM_AGENT_TIMEOUT_MULTIPLIER,
                agent_setup_timeout_multiplier=PREWARM_AGENT_SETUP_TIMEOUT_MULTIPLIER,
                environment_build_timeout_multiplier=(
                    PREWARM_ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER
                ),
                verifier_timeout_multiplier=PREWARM_VERIFIER_TIMEOUT_MULTIPLIER,
                agent_env={
                    "CC_HARNESS_VERIFIER_SMOKE_ONLY": "1",
                    "CC_HARNESS_AGENT_INSTALL_ONLY": "1",
                    "CC_HARNESS_VERIFIER_SMOKE_TIMEOUT_S": str(VERIFIER_SMOKE_TIMEOUT_S),
                    "CC_HARNESS_TERMINAL_VERIFIER_RUNTIME": "1",
                },
                extra_docker_compose_paths=(verifier_overlay,),
            )
            atomic_json(attempt_root / "command.json", command)
            emit(
                f"start {index}/{len(tasks)} {task.task_id} attempt={attempt} "
                "phase=verifier-smoke model_calls=0"
            )
            started = time.monotonic()
            try:
                return_code = _run_harbor_process(command, root, attempt_root, emit, task.task_id)
            except KeyboardInterrupt:
                store.interrupt_attempt(state, task.task_id, record)
                emit(f"interrupted {index}/{len(tasks)} {task.task_id} attempt={attempt}")
                _finalize(root, evidence_root, store, state, tasks)
                raise
            elapsed = round(time.monotonic() - started, 3)
            if evidence_jobs.exists():
                shutil.rmtree(evidence_jobs)
            shutil.copytree(jobs_dir, evidence_jobs)
            harbor_result = _inspect_harbor_result(attempt_root)
            status = (
                TrialStatus.PASS
                if return_code == 0 and harbor_result["success"]
                else TrialStatus.FAIL
            )
            diagnostic = _prewarm_diagnostic(attempt_root, harbor_result)
            result = {
                "schema_version": PREWARM_SCHEMA,
                "task_id": task.task_id,
                "attempt": attempt,
                "status": status.value,
                "return_code": return_code,
                "elapsed_seconds": elapsed,
                "model_calls": 0,
                "verifier_calls": 0,
                "dataset": TERMINAL_BENCH_21_DATASET,
                "harbor_version": HARBOR_VERSION,
                "install_only": False,
                "delete": True,
                "image_cache_reused_by_formal_run": True,
                "harbor_result": harbor_result,
                "verifier_smoke": {
                    "enabled": True,
                    "scored": False,
                    "agent_install": "full-frozen-offline",
                    "passed": status is TrialStatus.PASS,
                    "timeout_seconds": VERIFIER_SMOKE_TIMEOUT_S,
                    "baseline_reward_zero_accepted": True,
                },
                "diagnostic": diagnostic,
                "preflight_failure_class": (
                    _preflight_failure_class(diagnostic)
                    if status is TrialStatus.FAIL
                    else None
                ),
                "task_preflight": {
                    "docker_image_started": status is TrialStatus.PASS,
                    "verifier_imports": status is TrialStatus.PASS,
                    "test_sh_syntax_and_executable": status is TrialStatus.PASS,
                    "verifier_smoke": status is TrialStatus.PASS,
                    "tiktoken_cache": status is TrialStatus.PASS,
                    "data_disk_network": status is TrialStatus.PASS,
                    "timeout_config": True,
                },
            }
            result_path = attempt_root / "result.json"
            atomic_json(result_path, result)
            store.finish_attempt(state, task.task_id, record, status, result_path)
            emit(
                f"complete {index}/{len(tasks)} {task.task_id} status={status.value} "
                f"elapsed={elapsed:.1f}s model_calls=0"
            )
            trial = state["trials"][task.task_id]
            if (
                status is TrialStatus.FAIL
                and result.get("preflight_failure_class") != "environment_not_ready"
                and len(trial.get("attempts") or []) < attempt_budget
            ):
                backoff_seconds = min(30 * len(trial.get("attempts") or []), 120)
                emit(
                    f"retry {index}/{len(tasks)} {task.task_id} "
                    f"next_attempt={len(trial.get('attempts') or []) + 1} "
                    f"backoff={backoff_seconds}s"
                )
                trial["status"] = "pending"
                store.save(state)
                time.sleep(backoff_seconds)
            elif status is TrialStatus.FAIL:
                emit(
                    f"defer {index}/{len(tasks)} {task.task_id} "
                    f"class={result.get('preflight_failure_class') or 'unknown'}"
                )
                # Deterministic readiness failures cannot be repaired by
                # replaying the identical Harbor command. Preserve the first
                # attempt and move on; --retry-failed grants one fresh attempt
                # after the underlying environment has been repaired.
                break

    return _finalize(root, evidence_root, store, state, tasks)


def _freeze_prewarm_artifact(source: Path, target: Path) -> None:
    """Copy one immutable prewarm-only artifact into the evidence root."""

    if not source.is_file():
        raise FileNotFoundError(f"prewarm artifact is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if digest_file(target) != digest_file(source):
            raise ValueError(f"frozen prewarm artifact differs from prepared artifact: {target}")
        return
    temporary = target.with_suffix(target.suffix + ".copying")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def _inspect_harbor_result(attempt_root: Path) -> dict[str, Any]:
    """Read Harbor's job-level result instead of trusting its exit code.

    Harbor 0.20 can return exit code zero for ``--install-only`` even when an
    individual trial raised an exception.  The job result's ``stats`` object
    is the authoritative completion record for this no-model preflight.
    """

    jobs_root = attempt_root / "jobs"
    candidates = sorted(
        (
            path
            for path in jobs_root.glob("*/result.json")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        stats = payload.get("stats")
        if not isinstance(stats, Mapping):
            continue
        exception_types: set[str] = set()
        for evaluation in (stats.get("evals") or {}).values():
            if not isinstance(evaluation, Mapping):
                continue
            exception_stats = evaluation.get("exception_stats") or {}
            if isinstance(exception_stats, Mapping):
                exception_types.update(str(key) for key in exception_stats)
        n_total = _nonnegative_int(payload.get("n_total_trials"))
        n_completed = _nonnegative_int(stats.get("n_completed_trials"))
        n_errored = _nonnegative_int(stats.get("n_errored_trials"))
        n_cancelled = _nonnegative_int(stats.get("n_cancelled_trials"))
        n_running = _nonnegative_int(stats.get("n_running_trials"))
        n_pending = _nonnegative_int(stats.get("n_pending_trials"))
        return {
            "found": True,
            "path": path.relative_to(attempt_root).as_posix(),
            "n_total_trials": n_total,
            "n_completed_trials": n_completed,
            "n_errored_trials": n_errored,
            "n_cancelled_trials": n_cancelled,
            "n_running_trials": n_running,
            "n_pending_trials": n_pending,
            "exception_types": sorted(exception_types),
            "success": (
                n_total > 0
                and n_completed >= n_total
                and n_errored == 0
                and n_cancelled == 0
                and n_running == 0
                and n_pending == 0
            ),
        }
    return {
        "found": False,
        "path": None,
        "n_total_trials": 0,
        "n_completed_trials": 0,
        "n_errored_trials": 0,
        "n_cancelled_trials": 0,
        "n_running_trials": 0,
        "n_pending_trials": 0,
        "exception_types": [],
        "success": False,
    }


def _prewarm_diagnostic(
    attempt_root: Path,
    harbor_result: Mapping[str, Any],
) -> str:
    """Collect bounded evidence used to classify a failed preflight."""

    if bool(harbor_result.get("success")):
        return ""
    chunks: list[str] = []
    for name in ("harbor.stdout.log", "harbor.stderr.log"):
        path = attempt_root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                text = ""
            if text:
                chunks.append(f"{name}:\n{text[-6_000:]}")
    chunks.append("harbor_result: " + json.dumps(dict(harbor_result), ensure_ascii=False))
    jobs_root = attempt_root / "jobs"
    for path in jobs_root.glob("*/*/exception.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text:
            chunks.append(f"{path.relative_to(attempt_root).as_posix()}:\n{text[-6_000:]}")
    return "\n\n".join(chunks)[-16_000:]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _run_harbor_process(
    command: Sequence[str],
    project_root: Path,
    attempt_root: Path,
    emit: Callable[[str], None],
    task_id: str,
) -> int:
    stdout_path = attempt_root / "harbor.stdout.log"
    stderr_path = attempt_root / "harbor.stderr.log"
    environment = dict(os.environ)
    for secret_name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        environment.pop(secret_name, None)
    environment["PYTHONUTF8"] = "1"
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(project_root), existing_pythonpath) if value
    )
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            list(command),
            cwd=project_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            started = time.monotonic()
            last_heartbeat = started
            while process.poll() is None:
                time.sleep(1)
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    emit(
                        f"heartbeat task={task_id} phase=verifier-smoke "
                        f"pid={process.pid} process=running elapsed={int(now - started)}s "
                        "model_calls=0"
                    )
            return int(process.returncode or 0)
        except KeyboardInterrupt:
            _terminate_process_tree(process)
            raise


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _progress_writer(path: Path, callback: Progress | None) -> Callable[[str], None]:
    def emit(message: str) -> None:
        record = {"timestamp": utc_now(), "message": message}
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        if callback is not None:
            callback(message)
        else:
            print(f"[terminal-bench-prewarm] {message}", flush=True)

    return emit


def _preflight_failure_class(diagnostic: str) -> str:
    normalized = diagnostic.casefold()
    if any(
        marker in normalized
        for marker in (
            "modulenotfounderror",
            "no module named",
            "exceptiongroup",
            "tomli",
            "ctrf",
            "verifier test.sh",
            "verifier smoke",
            "cc_harness_verifier_smoke",
            "not executable",
        )
    ):
        return "environment_not_ready"
    return "infrastructure_pending"


def _last_prewarm_failure_class(evidence_root: Path, trial: Mapping[str, Any]) -> str | None:
    attempts = trial.get("attempts") or []
    if not attempts or not isinstance(attempts[-1], Mapping):
        return None
    result_rel = attempts[-1].get("result")
    if not isinstance(result_rel, str):
        return None
    path = evidence_root / result_rel
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    value = payload.get("preflight_failure_class")
    return str(value) if value else None


def _finalize(
    project_root: Path,
    evidence_root: Path,
    store: RunStateStore,
    state: dict[str, Any],
    tasks: Sequence[BenchmarkTask],
) -> dict[str, Path]:
    counts = {status.value: 0 for status in TrialStatus}
    for task in tasks:
        status = str(state["trials"].get(task.task_id, {}).get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    prepared = counts.get(TrialStatus.PASS.value, 0)
    complete = prepared == len(tasks)
    failed_records: list[dict[str, Any]] = []
    for task in tasks:
        trial = state["trials"].get(task.task_id) or {}
        attempts = trial.get("attempts") or []
        if not attempts:
            continue
        result_rel = attempts[-1].get("result")
        if not isinstance(result_rel, str):
            continue
        result_path = evidence_root / result_rel
        if result_path.is_file():
            try:
                failed_records.append(json.loads(result_path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                continue
    environment_not_ready_tasks = sorted(
        str(item.get("task_id"))
        for item in failed_records
        if item.get("preflight_failure_class") == "environment_not_ready"
    )
    infrastructure_pending_tasks = sorted(
        str(item.get("task_id"))
        for item in failed_records
        if item.get("preflight_failure_class") == "infrastructure_pending"
    )
    summary = {
        "schema_version": PREWARM_SCHEMA,
        "status": "ready" if complete else "incomplete",
        "dataset": TERMINAL_BENCH_21_DATASET,
        "harbor_version": HARBOR_VERSION,
        "wheel_sha256": digest_file(
            next((evidence_root / "frozen-inputs").glob("*.whl"))
        ),
        "harbor_plugin_sha256": terminal_plugin_digest(project_root),
        "offline_verifier_runtime": verifier_runtime_identity(
            verifier_runtime_cache_path(project_root)
        ),
        "execution_backend": inspect_terminal_host(project_root),
        "task_count": len(tasks),
        "prepared_tasks": prepared,
        "failed_tasks": counts.get(TrialStatus.FAIL.value, 0),
        "pending_tasks": counts.get("pending", 0),
        "interrupted_tasks": counts.get(TrialStatus.INTERRUPTED.value, 0),
        "model_calls": 0,
        "verifier_calls": 0,
        "task_preflight": {
            "docker_image_started": complete,
            "verifier_imports": complete,
            "test_sh_syntax_and_executable": complete,
            "verifier_smoke": complete,
            "tiktoken_cache": complete,
            "data_disk_network": complete,
            "timeout_config": True,
            "model_calls_before_preflight": 0,
        },
        "environment_not_ready_tasks": environment_not_ready_tasks,
        "infrastructure_pending_tasks": infrastructure_pending_tasks,
        "reuse": {
            "formal_run_creates_fresh_containers": True,
            "formal_run_reuses_content_addressed_images": True,
            "same_mutable_container_reused": False,
        },
    }
    atomic_json(evidence_root / "summary.json", summary)
    report = "\n".join(
        [
            "# Terminal-Bench 2.1 Docker prewarm",
            "",
            "- Mode: `Harbor full frozen-agent install (no model turn) + official verifier smoke`",
            "- Model calls: `0`",
            "- Verifier bootstrap: frozen local Python runtime; no per-task apt/Astral network bootstrap.",
            f"- Prepared: `{prepared}/{len(tasks)}` task environments",
            f"- Failed: `{summary['failed_tasks']}`",
            f"- Environment not ready: `{len(summary['environment_not_ready_tasks'])}`",
            f"- Infrastructure pending: `{len(summary['infrastructure_pending_tasks'])}`",
            "- Checks: Docker startup, verifier imports, non-scoring `/tests/test.sh` smoke, tiktoken cache, `/tests/test.sh` syntax/executable, disk/network, and timeout config.",
            "- Formal scoring will create fresh isolated containers and reuse the content-addressed Docker image/build cache.",
            "- The mutable prewarm container itself is not reused for scoring.",
            "",
        ]
    )
    atomic_text(evidence_root / "report.md", report)
    state["summary"] = summary
    state["finalized_at"] = utc_now() if complete else None
    store.save(state)
    indexed = [
        path
        for path in evidence_root.rglob("*")
        if path.is_file() and path.name != "integrity.json"
    ]
    atomic_json(
        evidence_root / "integrity.json",
        {
            "schema_version": "terminal-bench.prewarm-integrity.v1",
            "generated_at": utc_now(),
            "files": [
                {
                    "path": path.relative_to(evidence_root).as_posix(),
                    "sha256": digest_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in indexed
            ],
        },
    )
    return {
        "manifest": store.manifest_path,
        "catalog": store.catalog_path,
        "state": store.state_path,
        "summary": evidence_root / "summary.json",
        "report": evidence_root / "report.md",
        "integrity": evidence_root / "integrity.json",
        "raw": evidence_root / "raw",
    }


def _package_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("cc-harness")
    except PackageNotFoundError:
        return None
