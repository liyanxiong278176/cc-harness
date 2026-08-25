"""Detach-safe supervisor for the WSL2 Terminal-Bench runner.

The benchmark worker owns its own Linux process session and writes its log and
status to the Ubuntu ext4 filesystem.  Closing or interrupting the Windows
console therefore detaches the viewer instead of cancelling the scored task.
Re-running the same immutable command reattaches to the existing worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path("/mnt/d/agent_learning/cc-harness")
STATE_ROOT = Path.home() / ".local/state/cc-harness/terminal-bench-supervisor"
SYSTEMD_RUN = Path("/usr/bin/systemd-run")
SYSTEMCTL = Path("/usr/bin/systemctl")
_TERMINAL_STATES = {"complete", "failed", "interrupted"}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return b"terminal_bench_wsl_supervisor.py --worker" in command


def _identity(arguments: list[str]) -> str:
    kind = os.environ.get("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation")
    payload = json.dumps(
        {
            "kind": kind,
            "backend": os.environ.get("CC_HARNESS_TERMINAL_EXECUTION_BACKEND"),
            "project": str(PROJECT_ROOT),
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:20]


def _benchmark_command(arguments: list[str]) -> list[str]:
    if os.environ.get("CC_HARNESS_TERMINAL_SUPERVISOR_KIND") == "prepare":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts/prewarm_terminal_bench_2_1.py"),
            *arguments,
        ]
    return [
        sys.executable,
        str(PROJECT_ROOT / "scripts/run_cc_only_benchmark.py"),
        "terminal-bench-2.1",
        "--profile",
        "full",
        "--confirm-live",
        *arguments,
    ]


def _worker(run_root: Path, arguments: list[str]) -> int:
    status_path = run_root / "status.json"
    status = _read_json(status_path)
    status.update(
        {
            "state": "running",
            "worker_pid": os.getpid(),
            "started_at": status.get("started_at")
            or datetime.now(UTC).isoformat(),
            "arguments": arguments,
        }
    )
    _atomic_json(status_path, status)
    child: subprocess.Popen[bytes] | None = None
    stop_signal: int | None = None
    stop_requested_at: float | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop_signal, stop_requested_at
        if stop_signal is not None:
            return
        stop_signal = signum
        stop_requested_at = time.monotonic()
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, request_stop)
    try:
        child = subprocess.Popen(
            _benchmark_command(arguments),
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        while child.poll() is None:
            if (
                stop_requested_at is not None
                and time.monotonic() - stop_requested_at >= 10
            ):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            time.sleep(0.2)
        child_exit = int(child.returncode or 0)
        if stop_signal is not None:
            exit_code = 128 + stop_signal
            final_state = "interrupted"
            status["worker_error"] = (
                f"worker received signal {stop_signal}; child exit={child_exit}"
            )
        else:
            exit_code = child_exit
            final_state = "complete" if exit_code == 0 else "failed"
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = 1
        final_state = "failed"
        status["worker_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        status.update(
            {
                "state": final_state,
                "exit_code": exit_code,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        _atomic_json(status_path, status)
    return exit_code


def _follow(run_root: Path, pid: int) -> int:
    log_path = run_root / "worker.log"
    print(f"Terminal-Bench WSL worker pid={pid} log={log_path}", flush=True)
    print("Ctrl+C only detaches; run the identical command to reattach.", flush=True)
    position = 0
    stopping = False

    def detach(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, detach)
    signal.signal(signal.SIGTERM, detach)
    while not stopping:
        if log_path.is_file():
            with log_path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(position)
                chunk = stream.read()
                position = stream.tell()
            if chunk:
                print(chunk, end="", flush=True)
        status = _read_json(run_root / "status.json")
        if not _alive(pid):
            if status.get("state") not in _TERMINAL_STATES:
                status.update(
                    {
                        "state": "failed",
                        "exit_code": 1,
                        "worker_error": "worker exited without a terminal status",
                        "finished_at": datetime.now(UTC).isoformat(),
                    }
                )
                _atomic_json(run_root / "status.json", status)
            time.sleep(0.2)
            if log_path.is_file():
                with log_path.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(position)
                    chunk = stream.read()
                if chunk:
                    print(chunk, end="", flush=True)
            return int(status.get("exit_code") or 0)
        time.sleep(1)
    print(
        f"\nDetached. Worker {pid} is still running; evidence remains in {run_root}.",
        flush=True,
    )
    return 130


def _reconcile_dead_worker(status: dict[str, Any], *, detected_at: str) -> None:
    previous_state = str(status.get("state") or "unknown")
    previous_pid = int(status.get("worker_pid") or 0)
    if previous_state in {"running", "starting"}:
        status.setdefault("recovery_events", []).append(
            {
                "previous_state": previous_state,
                "previous_worker_pid": previous_pid,
                "reason": "stale-worker-recovered",
                "detected_at": detected_at,
            }
        )
    status.update({"state": "starting", "worker_pid": 0})
    for field in ("started_at", "finished_at", "exit_code", "worker_error"):
        status.pop(field, None)


def _rotate_stale_new_run_log(run_root: Path, arguments: list[str]) -> None:
    """Keep an earlier generation's output out of a fresh ``--new-run`` view."""

    if "--new-run" not in arguments:
        return
    log_path = run_root / "worker.log"
    if not log_path.is_file() or log_path.stat().st_size == 0:
        return
    archive = run_root / f"worker.previous-{time.time_ns()}.log"
    log_path.replace(archive)


def _worker_environment_arguments() -> list[str]:
    names = {
        "PATH",
        "PYTHONPATH",
        "WSL_DISTRO_NAME",
        "DOCKER_HOST",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    names.update(
        name
        for name in os.environ
        if name.startswith("CC_HARNESS_TERMINAL_")
        or name
        in {
            "CC_HARNESS_ALLOW_OBSERVABILITY_RESUME",
            "CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH",
        }
    )
    return [
        f"--setenv={name}={os.environ[name]}"
        for name in sorted(names)
        if os.environ.get(name)
    ]


def _start_systemd_worker(
    run_root: Path, arguments: list[str], *, identity: str
) -> tuple[int, str]:
    if not SYSTEMD_RUN.is_file() or not SYSTEMCTL.is_file():
        raise RuntimeError(
            "Terminal-Bench durable WSL worker requires the systemd user manager"
        )
    kind = os.environ.get("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation")
    unit = (
        f"cc-harness-terminal-{kind}-{identity}-{int(time.time() * 1000)}.service"
    )
    log_path = run_root / "worker.log"
    command = [
        str(SYSTEMD_RUN),
        "--user",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--property=Type=exec",
        "--property=Restart=no",
        f"--property=StandardOutput=append:{log_path}",
        f"--property=StandardError=append:{log_path}",
        f"--working-directory={PROJECT_ROOT}",
        "--setenv=PYTHONUNBUFFERED=1",
        *_worker_environment_arguments(),
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(run_root),
        *arguments,
    ]
    launched = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if launched.returncode != 0:
        detail = (launched.stderr or launched.stdout).strip()
        raise RuntimeError(f"systemd worker launch failed: {detail or launched.returncode}")
    for _ in range(100):
        shown = subprocess.run(
            [
                str(SYSTEMCTL),
                "--user",
                "show",
                unit,
                "--property=MainPID",
                "--value",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            pid = int(shown.stdout.strip())
        except ValueError:
            pid = 0
        if shown.returncode == 0 and pid > 1:
            return pid, unit
        time.sleep(0.1)
    raise RuntimeError(f"systemd worker unit did not publish MainPID: {unit}")


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["--worker"]:
        if len(arguments) < 2:
            raise SystemExit("worker state directory is required")
        return _worker(Path(arguments[1]), arguments[2:])

    # Read-only checks do not need a persistent background worker.
    if "--check" in arguments or "--preflight-only" in arguments:
        return subprocess.run(
            _benchmark_command(arguments), cwd=PROJECT_ROOT, check=False
        ).returncode

    kind = os.environ.get("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation")
    run_root = STATE_ROOT / kind / _identity(arguments)
    run_root.mkdir(parents=True, exist_ok=True)
    status_path = run_root / "status.json"
    status = _read_json(status_path)
    existing_pid = int(status.get("worker_pid") or 0)
    if _alive(existing_pid):
        print("Reattaching to the existing immutable Terminal-Bench run.", flush=True)
        return _follow(run_root, existing_pid)

    log_path = run_root / "worker.log"
    detected_at = datetime.now(UTC).isoformat()
    _reconcile_dead_worker(status, detected_at=detected_at)
    _rotate_stale_new_run_log(run_root, arguments)
    status.update(
        {
            "created_at": status.get("created_at") or detected_at,
            "arguments": arguments,
            "log_path": str(log_path),
        }
    )
    _atomic_json(status_path, status)
    try:
        worker_pid, unit = _start_systemd_worker(
            run_root, arguments, identity=_identity(arguments)
        )
    except Exception as exc:
        status.update(
            {
                "state": "failed",
                "exit_code": 1,
                "worker_error": f"{type(exc).__name__}: {exc}",
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        _atomic_json(status_path, status)
        raise
    status.update({"worker_pid": worker_pid, "systemd_unit": unit})
    _atomic_json(status_path, status)
    return _follow(run_root, worker_pid)


if __name__ == "__main__":
    raise SystemExit(main())
