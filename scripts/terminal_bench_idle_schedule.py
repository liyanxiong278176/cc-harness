"""Pause and resume one Terminal-Bench run around DeepSeek idle windows.

This is a foreground controller for a single immutable result root.  It does
not create a second benchmark run: an existing worker is stopped at a window
boundary and resumed later with ``--output-root`` so the frozen catalog,
attempts, and evidence remain the same.  The controller deliberately refuses
to start when the result root is missing or already complete.

DeepSeek idle windows (local machine time, Asia/Shanghai in the supported
launcher) are:

* Monday-Friday 00:00-09:00, 12:00-14:00, and 18:00-24:00
* Saturday-Sunday all day

The scheduler is intentionally separate from Windows Task Scheduler.  It is
an attached/foreground process that can be watched alongside the supervisor;
closing its viewer does not kill the systemd worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path("/mnt/d/agent_learning/cc-harness")
SUPERVISOR = PROJECT_ROOT / "scripts/terminal_bench_wsl_supervisor.py"
STATE_ROOT = Path.home() / ".local/state/cc-harness/terminal-bench-supervisor"
SYSTEMCTL = Path("/usr/bin/systemctl")
TERMINAL_STATES = {"complete", "failed", "interrupted"}
RETRYABLE_ENVIRONMENT_CLASSES = {"environment_not_ready", "deterministic"}
MAX_ENVIRONMENT_RETRIES = 10


def is_deepseek_idle(now: datetime | None = None) -> bool:
    """Return whether ``now`` falls inside the configured local idle window."""

    current = now or datetime.now().astimezone()
    if current.weekday() >= 5:
        return True
    hour = current.hour
    return hour < 9 or 12 <= hour < 14 or hour >= 18


def next_window_boundary(now: datetime | None = None) -> datetime:
    """Return the next local boundary at which the idle state can change."""

    current = now or datetime.now().astimezone()
    candidates: list[datetime] = []
    for day_offset in range(0, 8):
        day = current.date()
        if day_offset:
            day = day.fromordinal(day.toordinal() + day_offset)
        for hour, minute in ((0, 0), (9, 0), (12, 0), (14, 0), (18, 0)):
            candidate = current.replace(
                year=day.year,
                month=day.month,
                day=day.day,
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if candidate > current:
                candidates.append(candidate)
    return min(candidates)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_output_root(value: Path) -> Path:
    """Accept both WSL paths and the Windows path users see in the launcher."""

    raw = str(value)
    if len(raw) >= 3 and raw[1:3] == ":\\":
        drive = raw[0].lower()
        relative = raw[3:].replace("\\", "/")
        return Path(f"/mnt/{drive}/{relative}")
    if not raw.startswith("/"):
        return (PROJECT_ROOT / value).resolve()
    return value


def _identity(arguments: list[str]) -> str:
    payload = json.dumps(
        {
            "kind": os.environ.get("CC_HARNESS_TERMINAL_SUPERVISOR_KIND", "evaluation"),
            "backend": os.environ.get("CC_HARNESS_TERMINAL_EXECUTION_BACKEND"),
            "project": str(PROJECT_ROOT),
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:20]


def _status_path(arguments: list[str]) -> Path:
    return STATE_ROOT / "evaluation" / _identity(arguments) / "status.json"


def _systemd_active(unit: str | None) -> bool:
    if not unit or not SYSTEMCTL.is_file():
        return False
    result = subprocess.run(
        [str(SYSTEMCTL), "--user", "is-active", "--quiet", unit],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _stop_worker(status: dict[str, Any]) -> bool:
    unit = str(status.get("systemd_unit") or "")
    if not unit:
        return False
    result = subprocess.run(
        [str(SYSTEMCTL), "--user", "stop", unit],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _result_counts(root: Path) -> dict[str, int]:
    state = _read_json(root / "state.json")
    values = [value for value in (state.get("trials") or {}).values() if isinstance(value, dict)]
    return {
        status: sum(1 for value in values if value.get("status") == status)
        for status in ("pass", "fail", "running", "pending", "invalid")
    }


def _complete(root: Path) -> bool:
    counts = _result_counts(root)
    total = sum(counts.values())
    reports_ready = all((root / name).is_file() for name in ("summary.json", "report.md", "integrity.json"))
    return (
        total > 0
        and reports_ready
        and counts["pass"] + counts["fail"] == total
        and not (counts["running"] or counts["pending"] or counts["invalid"])
    )


def _environment_retry_count(trial: dict[str, Any]) -> int:
    """Count retained environment failures for one logical task attempt."""

    count = 0
    for attempt in trial.get("attempts") or ():
        if not isinstance(attempt, dict):
            continue
        if attempt.get("infrastructure_class") in RETRYABLE_ENVIRONMENT_CLASSES:
            count += 1
        count += len(attempt.get("infrastructure_failures") or ())
    return count


def _blocking_infrastructure(
    root: Path, *, allow_environment_retry: bool = False
) -> str | None:
    """Return a reason that requires human repair instead of auto-resume.

    Environment-not-ready evidence can be retried only when the operator
    explicitly opts in. The scheduler still bounds those retries and never
    replays ordinary invalid or scored task outcomes.
    """

    state = _read_json(root / "state.json")
    retryable_pending = False
    for task_id, trial in (state.get("trials") or {}).items():
        if not isinstance(trial, dict):
            continue
        if trial.get("status") == "invalid":
            return f"invalid task={task_id}"
        if trial.get("status") == "pending" and trial.get("infrastructure_class"):
            infrastructure_class = str(trial.get("infrastructure_class"))
            if (
                allow_environment_retry
                and infrastructure_class in RETRYABLE_ENVIRONMENT_CLASSES
            ):
                retry_count = _environment_retry_count(trial)
                if retry_count >= MAX_ENVIRONMENT_RETRIES:
                    return (
                        "environment retry budget exhausted "
                        f"task={task_id} retries={retry_count}"
                    )
                retryable_pending = True
                continue
            return f"infrastructure_pending task={task_id} class={trial.get('infrastructure_class')}"
    for event in state.get("operational_pauses") or ():
        if not isinstance(event, dict):
            continue
        reason = str(event.get("reason") or "")
        if reason == "cost_limit":
            return f"manual_pause reason={reason}"
        if reason.startswith("terminal_infrastructure") and not (
            allow_environment_retry and retryable_pending
        ):
            return f"manual_pause reason={reason}"
    return None


def _log(message: str, stream: Any, log_path: Path) -> None:
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}\n"
    stream.write(line)
    stream.flush()
    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(line)
    except OSError:
        pass


def run(
    root: Path,
    *,
    initial_status: Path | None,
    poll_seconds: float,
    retry_environment: bool,
) -> int:
    root = _normalize_output_root(root)
    if initial_status is not None:
        initial_status = _normalize_output_root(initial_status)
    if not (root / "manifest.json").is_file() or not (root / "state.json").is_file():
        raise SystemExit(f"refusing to schedule an incomplete result root: {root}")

    log_path = root / "idle-scheduler.log"
    resume_args = ["--output-root", str(root), "--confirm-live"]
    if retry_environment:
        resume_args.append("--retry-invalid")
    resume_status = _status_path(resume_args)
    status_path = initial_status or resume_status
    paused_by_schedule = False
    stopping = False
    launch_process: subprocess.Popen[bytes] | None = None

    def detach(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    signal.signal(signal.SIGINT, detach)
    signal.signal(signal.SIGTERM, detach)
    try:
        _log(f"idle-window controller attached root={root}", sys.stdout, log_path)
        while not stopping:
            now = datetime.now().astimezone()
            status = _read_json(status_path)
            if launch_process is not None and launch_process.poll() is not None:
                launch_process = None
            active = bool(launch_process and launch_process.poll() is None) or _systemd_active(
                str(status.get("systemd_unit") or "")
            )
            complete = _complete(root)
            blocking = _blocking_infrastructure(
                root, allow_environment_retry=retry_environment
            )
            if blocking:
                if active:
                    _stop_worker(status)
                _log(f"controller stopped for manual infrastructure analysis: {blocking}", sys.stdout, log_path)
                return 2
            if complete:
                _log("result root complete; controller exiting", sys.stdout, log_path)
                return 0
            if not is_deepseek_idle(now):
                if active:
                    if _stop_worker(status):
                        paused_by_schedule = True
                        _log(
                            f"outside idle window; stopped unit={status.get('systemd_unit')}",
                            sys.stdout,
                            log_path,
                        )
                    else:
                        _log("outside idle window; failed to stop active unit", sys.stdout, log_path)
                        return 2
                elif status.get("state") == "failed" and not paused_by_schedule:
                    _log("worker failed outside a scheduled pause; controller exiting", sys.stdout, log_path)
                    return 1
                boundary = next_window_boundary(now)
                _log(
                    f"outside idle window; waiting until {boundary.isoformat(timespec='minutes')}",
                    sys.stdout,
                    log_path,
                )
            else:
                if not active:
                    state = str(status.get("state") or "")
                    if state == "failed" and not paused_by_schedule:
                        _log("worker failed; controller will not auto-retry", sys.stdout, log_path)
                        return 1
                    if state in TERMINAL_STATES or paused_by_schedule or state in {"", "starting"}:
                        command = [sys.executable, str(SUPERVISOR), *resume_args]
                        launch_process = subprocess.Popen(
                            command,
                            cwd=PROJECT_ROOT,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"},
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        status_path = resume_status
                        paused_by_schedule = False
                        _log("idle window open; resumed the same output root", sys.stdout, log_path)
                _log("idle window open; worker active or starting", sys.stdout, log_path)
            time.sleep(max(1.0, poll_seconds))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--initial-status",
        type=Path,
        help="status.json for a worker started before this controller (used once)",
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument(
        "--retry-environment",
        action="store_true",
        help=(
            "retry pending environment_not_ready/deterministic evidence in the "
            f"same logical task, bounded at {MAX_ENVIRONMENT_RETRIES} attempts"
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    return run(
        args.output_root,
        initial_status=args.initial_status,
        poll_seconds=args.poll_seconds,
        retry_environment=args.retry_environment,
    )


if __name__ == "__main__":
    raise SystemExit(main())
