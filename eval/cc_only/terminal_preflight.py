"""Non-scoring Terminal-Bench 2.1 preflight gates."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from eval.harbor.paired import HARBOR_VERSION

from .adapters.harbor import TERMINAL_BENCH_21_DATASET
from .launch import final_result, run_cc_prompt
from .storage import atomic_json, digest_file, read_json, utc_now
from .terminal_host import require_terminal_host

# Keep the oracle gate on a task that is present in the pinned 2.1 registry
# package as well as the frozen local catalog.  ``cancel-async-tasks`` was
# present in an earlier registry snapshot but is not resolvable by the current
# Harbor package metadata, which made the gate fail before Docker was started.
ORACLE_TASK = "compile-compcert"
ORACLE_HARBOR_TASK = f"terminal-bench/{ORACLE_TASK}"
FORMAL_NETWORK_PROBE_IMAGE = "alexgshaw/bn-fit-modify:20251031"
FORMAL_NETWORK_PROBE_ATTEMPTS = 3
FORMAL_NETWORK_PROBE_SCRIPT = r"""
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
source /root/.local/bin/env
uvx \
  -p 3.13 \
  -w pytest==8.4.1 \
  -w pandas==2.3.2 \
  -w scipy==1.16.1 \
  -w pytest-json-ctrf==0.3.5 \
  pytest --version >/dev/null
""".strip()


def gate_root(project_root: Path) -> Path:
    return (
        project_root
        / "eval"
        / "result"
        / "cc-only"
        / "terminal-bench-2.1"
        / "deepseek-v4-flash"
    )


def wheel_path(project_root: Path) -> Path:
    return (
        project_root
        / "eval"
        / "result"
        / "cc-only"
        / "_artifacts"
        / "cc_harness-0.1.0-py3-none-any.whl"
    )


def run_oracle_preflight(project_root: Path) -> dict[str, Path]:
    require_terminal_host(project_root)
    root = gate_root(project_root) / "oracle-preflight"
    wheel = wheel_path(project_root)
    identity = _identity(wheel)
    existing = root / "result.json"
    if existing.is_file():
        result = read_json(existing)
        if result.get("status") == "pass" and _oracle_result_compatible(result, identity):
            # The official oracle runs Harbor's built-in solution and never
            # installs the cc-harness wheel.  Rebind a passing oracle result
            # to a newly built wheel instead of paying the ~20 minute cold
            # CompCert build on every unrelated harness edit.  Keep the
            # original attempt untouched and record the provenance in the
            # selected result/manifest so this is auditable, not a fabricated
            # score.
            if result.get("identity") != identity:
                previous_identity = result.get("identity")
                result = {
                    **result,
                    "identity": identity,
                    "reused_at": utc_now(),
                    "reused_from_identity": previous_identity,
                }
                atomic_json(existing, result)
                manifest = root / "manifest.json"
                if manifest.is_file():
                    manifest_payload = read_json(manifest)
                    atomic_json(
                        manifest,
                        {
                            **manifest_payload,
                            "identity": identity,
                            "reused_at": result["reused_at"],
                            "reused_from_identity": previous_identity,
                        },
                    )
                _write_integrity(root)
            return _paths(root)
    root.mkdir(parents=True, exist_ok=True)
    attempts = root / "attempts"
    attempts.mkdir(exist_ok=True)
    attempt = len(tuple(attempts.glob("attempt-*"))) + 1
    attempt_root = attempts / f"attempt-{attempt}"
    attempt_root.mkdir()
    if existing.is_file():
        shutil.copy2(existing, attempt_root / "previous-selected-result.json")
    jobs = attempt_root / "jobs"
    jobs.mkdir(exist_ok=True)
    command = [
        str(shutil.which("uvx") or "uvx"),
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "harbor",
        "run",
        "--dataset",
        TERMINAL_BENCH_21_DATASET,
        "--include-task-name",
        ORACLE_HARBOR_TASK,
        "--agent",
        "oracle",
        "--n-attempts",
        "1",
        "--n-concurrent",
        "1",
        "--jobs-dir",
        str(jobs),
        "--quiet",
        "--yes",
    ]
    atomic_json(
        root / "manifest.json",
        {
            "schema_version": "terminal-bench.oracle-preflight.v1",
            "created_at": utc_now(),
            "identity": identity,
            "task": ORACLE_HARBOR_TASK,
            "attempt": attempt,
            "command": command,
            "model_calls": 0,
        },
    )
    completed = subprocess.run(
        command,
        cwd=project_root,
        # Harbor/Rich renders a summary after the Docker job.  Force UTF-8 so
        # the Windows host's GBK console cannot turn a successful oracle job
        # into a false gate failure during that reporting step.
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    (attempt_root / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (attempt_root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    job_results = sorted(
        path for path in jobs.rglob("result.json") if path.parent.parent == jobs
    )
    reward = None
    errored = True
    if len(job_results) == 1:
        payload = read_json(job_results[0])
        stats = payload.get("stats") or {}
        errored = bool(int(stats.get("n_errored_trials") or 0))
        reward = _reward(stats)
    passed = completed.returncode == 0 and not errored and reward is not None and reward > 0
    result = {
            "schema_version": "terminal-bench.oracle-preflight-result.v1",
            "status": "pass" if passed else "fail",
            "finished_at": utc_now(),
            "identity": identity,
            "task": ORACLE_TASK,
            "harbor_task": ORACLE_HARBOR_TASK,
            "attempt": attempt,
            "reward": reward,
            "exit_code": completed.returncode,
            "model_calls": 0,
        }
    atomic_json(attempt_root / "result.json", result)
    atomic_json(existing, result)
    _write_integrity(root)
    if not passed:
        raise RuntimeError("Terminal-Bench official-oracle preflight failed; inspect its result directory")
    return _paths(root)


async def run_synthetic_canary(project_root: Path) -> dict[str, Path]:
    require_terminal_host(project_root)
    root = gate_root(project_root) / "synthetic-canary"
    wheel = wheel_path(project_root)
    identity = _identity(wheel)
    selected = root / "result.json"
    if selected.is_file():
        result = read_json(selected)
        if result.get("status") == "pass":
            if result.get("identity") == identity:
                return _paths(root)
            # The synthetic canary exercises the host harness and does not
            # install the frozen Terminal-Bench wheel.  A wheel-only refresh
            # therefore does not invalidate a passing canary, but rebinding it
            # must be explicit and leave provenance in the gate evidence.
            previous_identity = result.get("identity") or {}
            compatible = isinstance(previous_identity, dict) and all(
                previous_identity.get(field) == identity[field]
                for field in ("dataset", "harbor_version", "mode", "model")
            )
            if compatible and os.environ.get("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH") == "1":
                reused_at = utc_now()
                result = {
                    **result,
                    "identity": identity,
                    "reused_at": reused_at,
                    "reused_from_identity": previous_identity,
                    "reuse_reason": "authorized-frozen-wheel-refresh",
                }
                atomic_json(selected, result)
                manifest = root / "manifest.json"
                if manifest.is_file():
                    manifest_payload = read_json(manifest)
                    atomic_json(
                        manifest,
                        {
                            **manifest_payload,
                            "identity": identity,
                            "reused_at": reused_at,
                            "reused_from_identity": previous_identity,
                            "reuse_reason": "authorized-frozen-wheel-refresh",
                        },
                    )
                _write_integrity(root)
                return _paths(root)
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(
        root / "manifest.json",
        {
            "schema_version": "terminal-bench.synthetic-canary.v1",
            "created_at": utc_now(),
            "identity": identity,
            "official_task": False,
            "scored": False,
        },
    )
    attempts = root / "attempts"
    attempts.mkdir(exist_ok=True)
    attempt = len(tuple(attempts.glob("attempt-*"))) + 1
    attempt_root = attempts / f"attempt-{attempt}"
    workspace = attempt_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "input.txt").write_text("value=41\n", encoding="utf-8")
    prompt = (
        "This is a synthetic terminal canary outside Terminal-Bench. In the current workspace, "
        "change input.txt to exactly 'value=42' followed by a newline, create evidence.txt with "
        "exactly 'SYNTHETIC_CANARY_OK' followed by a newline, verify both files, then reply exactly "
        "SYNTHETIC_CANARY_OK."
    )
    completed = await run_cc_prompt(
        project_root,
        workspace,
        attempt_root,
        prompt,
        capability_profile="clean-coding",
        home=attempt_root / "home",
        watchdog_seconds=600,
        permission_mode="bypass-prompts",
        host_execution=True,
        mode="coding",
        environment_overrides={
            "MEMORY_ENABLED": "false",
            "CC_HARNESS_OUTPUT_EGRESS_GUARD": "1",
        },
    )
    parsed: dict[str, Any] = {}
    try:
        parsed = final_result(completed.stdout)
    except (UnicodeError, ValueError):
        pass
    passed = (
        completed.evidence.valid_for_parity
        and parsed.get("resolved_model") == "deepseek-v4-flash"
        and str(parsed.get("text") or "").strip() == "SYNTHETIC_CANARY_OK"
        and (workspace / "input.txt").read_text(encoding="utf-8") == "value=42\n"
        and (workspace / "evidence.txt").read_text(encoding="utf-8")
        == "SYNTHETIC_CANARY_OK\n"
    )
    result = {
        "schema_version": "terminal-bench.synthetic-canary-result.v1",
        "status": "pass" if passed else "fail",
        "finished_at": utc_now(),
        "identity": identity,
        "attempt": attempt,
        "model": parsed.get("resolved_model"),
        "usage": parsed.get("usage") or {},
        "trajectory_present": bool(parsed.get("trajectory")),
        "tool_calls": int((parsed.get("usage") or {}).get("tool_calls") or 0),
    }
    atomic_json(attempt_root / "result.json", result)
    atomic_json(selected, result)
    _write_integrity(root)
    if not passed:
        raise RuntimeError("Terminal-Bench synthetic live canary failed; inspect its result directory")
    return _paths(root)


def require_formal_gates(
    project_root: Path,
    task_limit: int | None = None,
    *,
    frozen_wheel: Path | None = None,
) -> None:
    """Require only the non-mutating readiness check used by the official path.

    The official protocol does not require a synthetic canary, verifier
    replacement, or install-only prewarm.  Optional oracle runs are diagnostic
    and must not become an extra condition that changes the scored run.
    """

    expected_task_count = 89 if task_limit is None else int(task_limit)
    require_terminal_host(project_root)
    if expected_task_count < 1 or expected_task_count > 89:
        raise ValueError("Terminal-Bench task_limit must be between 1 and 89")
    wheel = frozen_wheel or wheel_path(project_root)
    expected = _identity(wheel)
    root = gate_root(project_root)
    check = root / "check" / "summary.json"
    if not check.is_file():
        raise RuntimeError(f"Terminal-Bench local readiness check is missing: {check}")
    check_summary = read_json(check)
    if check_summary.get("status") != "ready":
        raise RuntimeError("Terminal-Bench zero-model check is not ready")
    check_manifest = read_json(root / "check" / "manifest.json")
    check_identity = (check_manifest.get("adapter_run_identity") or {})
    if check_identity.get("wheel_sha256") != expected["wheel_sha256"]:
        raise RuntimeError("Terminal-Bench zero-model check used a different frozen wheel")
    require_formal_verifier_network(project_root)


def require_formal_verifier_network(project_root: Path) -> None:
    """Probe verifier bootstrap endpoints from an official task image.

    Terminal-Bench 2.1's pinned official verifiers fetch ``uv`` and Python
    packages during grading.  A host-level TCP check is insufficient because
    Docker may have different proxy/DNS routing.  This gate changes neither
    the task nor verifier: it runs a disposable copy of an already prepared
    official image and refuses to spend model tokens until the same endpoint
    path used by the official verifier is reachable.
    """

    image = os.environ.get(
        "CC_HARNESS_TERMINAL_NETWORK_PROBE_IMAGE", FORMAL_NETWORK_PROBE_IMAGE
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        image,
        "bash",
        "-lc",
        FORMAL_NETWORK_PROBE_SCRIPT,
    ]
    errors: list[str] = []
    for attempt in range(1, FORMAL_NETWORK_PROBE_ATTEMPTS + 1):
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=900,
            )
        except subprocess.TimeoutExpired as exc:
            detail = "\n".join(
                str(value or "") for value in (exc.stdout, exc.stderr)
            ).strip()
            errors.append(f"attempt {attempt}: timed out; {detail[-2_000:]}")
        else:
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "unknown error").strip()
                errors.append(f"attempt {attempt}: {detail[-2_000:]}")
        if attempt < FORMAL_NETWORK_PROBE_ATTEMPTS:
            time.sleep(5 * attempt)
    if errors:
        raise RuntimeError(
            "Terminal-Bench official verifier network is not ready inside Docker; "
            "all three full verifier-bootstrap soak attempts must pass before model calls. "
            + " | ".join(errors)
        )


def _identity(wheel: Path) -> dict[str, Any]:
    if not wheel.is_file():
        raise FileNotFoundError(f"prepared cc-harness wheel is missing: {wheel}")
    return {
        "dataset": TERMINAL_BENCH_21_DATASET,
        "harbor_version": HARBOR_VERSION,
        "wheel_sha256": digest_file(wheel),
        "model": "deepseek-v4-flash",
        "mode": "coding",
    }


def _oracle_result_compatible(result: dict[str, Any], identity: dict[str, Any]) -> bool:
    """Whether a passing oracle result can be reused for a new wheel build."""

    previous = result.get("identity")
    return (
        result.get("task") == ORACLE_TASK
        and result.get("harbor_task") == ORACLE_HARBOR_TASK
        and isinstance(previous, dict)
        and all(previous.get(field) == identity[field] for field in ("dataset", "harbor_version", "mode", "model"))
    )


def _reward(stats: dict[str, Any]) -> float | None:
    for evaluation in (stats.get("evals") or {}).values():
        metrics = evaluation.get("metrics") or []
        if metrics and isinstance(metrics[0].get("mean"), (int, float)):
            return float(metrics[0]["mean"])
    return None


def _write_integrity(root: Path) -> None:
    files = [path for path in root.rglob("*") if path.is_file() and path.name != "integrity.json"]
    atomic_json(
        root / "integrity.json",
        {
            "schema_version": "terminal-bench.preflight-integrity.v1",
            "generated_at": utc_now(),
            "files": [
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": digest_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in sorted(files)
            ],
        },
    )


def _paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "manifest": root / "manifest.json",
        "result": root / "result.json",
        "integrity": root / "integrity.json",
    }
