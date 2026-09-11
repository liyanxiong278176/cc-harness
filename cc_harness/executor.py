"""执行加固:对放行的 run_command 限制爆破半径。

cwd 锁项目根、env 剥离密钥(L7「凭证不可达」可移植版)、默认 30s 超时；
隔离 benchmark 可通过受限环境覆盖获得更长的构建预算。Executor 协议预留,
后续可插 Docker/bubblewrap 真沙箱。
"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import locale
import math
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cc_harness.mcp_client import ToolResult

_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|CREDENTIAL|PASSWORD|API)", re.IGNORECASE)
RUN_COMMAND_TIMEOUT_S = 30
# Long-running benchmark builds need a larger per-call budget than the normal
# interactive shell default.  The override is intentionally opt-in and capped:
# a benchmark cannot turn a hung command into an unbounded subprocess.
MAX_RUN_COMMAND_TIMEOUT_S = 1_800
RUN_COMMAND_TIMEOUT_ENV = "CC_HARNESS_RUN_COMMAND_TIMEOUT_S"
RUN_COMMAND_PROGRESS_FILE_ENV = "CC_HARNESS_PROGRESS_FILE"
RUN_COMMAND_HEARTBEAT_ENV = "CC_HARNESS_PROGRESS_HEARTBEAT_S"
RUN_COMMAND_HEARTBEAT_S = 15
RUN_COMMAND_IDLE_TIMEOUT_ENV = "CC_HARNESS_RUN_COMMAND_IDLE_TIMEOUT_S"
MAX_RUN_COMMAND_IDLE_TIMEOUT_S = 1_800
TASK_DEADLINE_EPOCH_ENV = "CC_HARNESS_TASK_DEADLINE_EPOCH"
TASK_DEADLINE_RESERVE_S_ENV = "CC_HARNESS_TASK_DEADLINE_RESERVE_S"
BACKGROUND_PROCESS_SCHEMA = "cc-harness.background-process.v1"
BACKGROUND_READINESS_TIMEOUT_S = 15.0
MAX_BACKGROUND_READINESS_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class ManagedServiceContract:
    """Explicit lifecycle contract for a process that must outlive a turn."""

    name: str
    command: str
    readiness_command: str | None = None
    health_command: str | None = None
    readiness_timeout_s: float = BACKGROUND_READINESS_TIMEOUT_S
    health_timeout_s: float = 2.0
    shutdown_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.command.strip():
            raise ValueError("managed service name and command are required")
        for field_name in ("readiness_timeout_s", "health_timeout_s", "shutdown_timeout_s"):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be a positive finite number")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "readiness_command": self.readiness_command,
            "health_command": self.health_command,
            "readiness_timeout_s": self.readiness_timeout_s,
            "health_timeout_s": self.health_timeout_s,
            "shutdown_timeout_s": self.shutdown_timeout_s,
        }


def resolve_run_command_timeout(
    value: int | float | str | None = None,
    *,
    default: int | float = RUN_COMMAND_TIMEOUT_S,
) -> float:
    """Resolve a command timeout while preserving a finite hard upper bound.

    The regular interactive path remains 30 seconds.  Benchmark adapters may
    pass a larger value through ``CC_HARNESS_RUN_COMMAND_TIMEOUT_S`` (or an
    explicit constructor value), but malformed, non-positive, non-finite and
    over-limit values fail back to the safe default/cap respectively.
    """

    raw: int | float | str | None = value
    if raw is None:
        raw = os.getenv(RUN_COMMAND_TIMEOUT_ENV)
    try:
        parsed = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(parsed) or parsed <= 0:
        return float(default)
    return min(parsed, float(MAX_RUN_COMMAND_TIMEOUT_S))


def _output_chunks(text: str, *, chunk_size: int = 16_384, max_chars: int = 128_000) -> list[str]:
    """Keep bounded stdout/stderr chunks as durable command evidence."""

    bounded = text[:max_chars]
    return [bounded[index : index + chunk_size] for index in range(0, len(bounded), chunk_size)]


def _resolve_progress_heartbeat(value: str | None = None) -> float:
    try:
        parsed = float(value) if value is not None else float(RUN_COMMAND_HEARTBEAT_S)
    except (TypeError, ValueError):
        return float(RUN_COMMAND_HEARTBEAT_S)
    if not math.isfinite(parsed) or parsed <= 0:
        return float(RUN_COMMAND_HEARTBEAT_S)
    return min(parsed, 60.0)


def resolve_run_command_idle_timeout(value: int | float | str | None = None) -> float | None:
    """Resolve an optional no-output watchdog for long-running commands.

    It is opt-in so ordinary interactive commands that intentionally produce
    no output (for example ``sleep``) keep their historical behavior.  A
    benchmark or deployment may enable it through the environment while the
    total command timeout remains the primary upper bound.
    """

    raw: int | float | str | None = value
    if raw is None:
        raw = os.getenv(RUN_COMMAND_IDLE_TIMEOUT_ENV)
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return min(parsed, float(MAX_RUN_COMMAND_IDLE_TIMEOUT_S))


def remaining_task_budget(*, now: float | None = None) -> float | None:
    """Return the task budget available to commands after a finalization reserve."""

    raw_deadline = os.getenv(TASK_DEADLINE_EPOCH_ENV)
    if raw_deadline is None:
        return None
    try:
        deadline = float(raw_deadline)
        reserve = max(0.0, float(os.getenv(TASK_DEADLINE_RESERVE_S_ENV, "90")))
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if not math.isfinite(deadline) or not math.isfinite(reserve):
        return None
    return max(1.0, deadline - current - reserve)


def _command_digest(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


def _process_start_token(pid: int) -> str | None:
    """Return a stable process-start identity when the host exposes one.

    A PID alone is not a safe handle: after a service exits the operating
    system can reuse its number for an unrelated process.  Linux exposes the
    start tick in ``/proc/<pid>/stat`` and Windows exposes a creation time via
    psutil when installed.  The persisted background manifest stores this
    token and refuses to operate on a reused PID.
    """

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid < 1:
        return None
    if os.name == "posix":
        try:
            # ``comm`` may contain spaces/parentheses; split only after its
            # closing parenthesis so field 22 (starttime) remains stable.
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            _, _, remainder = raw.rpartition(")")
            fields = remainder.split()
            # Field 3 is the process state.  ``kill(pid, 0)`` succeeds for a
            # zombie until its parent reaps it, but a zombie cannot service a
            # background command.  Return no identity for it.
            if fields and fields[0] == "Z":
                return None
            return fields[19] if len(fields) > 19 else None
        except (OSError, UnicodeError, IndexError):
            return None
    try:
        import psutil  # type: ignore[import-not-found]

        return str(psutil.Process(pid).create_time())
    except Exception:
        # Process identity is telemetry only.  psutil can raise platform-
        # specific errors (NoSuchProcess, AccessDenied, zombie races, etc.);
        # never let those escape into command execution or recovery logic.
        return None


def _process_alive(pid: int) -> bool:
    """Best-effort liveness check that treats permission as alive."""

    try:
        if os.name == "posix":
            pid_value = int(pid)
            os.kill(pid_value, 0)
            try:
                raw = Path(f"/proc/{pid_value}/stat").read_text(encoding="utf-8")
                _, _, remainder = raw.rpartition(")")
                fields = remainder.split()
                if fields and fields[0] == "Z":
                    return False
            except (OSError, UnicodeError, IndexError):
                # A host without procfs is still covered by kill(0).
                pass
            return True
        import psutil  # type: ignore[import-not-found]

        process = psutil.Process(int(pid))
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        # On Windows without psutil, the safe answer is unknown/alive.  The
        # start token check still prevents a known PID reuse where available.
        return True
    except PermissionError:
        return True
    except Exception:
        # Liveness is best effort.  Treat an unexpected psutil/platform error
        # as not alive so stale handles are not reused accidentally.
        return False


def _resolve_background_readiness_timeout(value: Any) -> float:
    try:
        parsed = float(value) if value is not None else BACKGROUND_READINESS_TIMEOUT_S
    except (TypeError, ValueError):
        parsed = BACKGROUND_READINESS_TIMEOUT_S
    if not math.isfinite(parsed) or parsed <= 0:
        parsed = BACKGROUND_READINESS_TIMEOUT_S
    return min(parsed, MAX_BACKGROUND_READINESS_TIMEOUT_S)


def _workspace_activity(root: Path, *, limit: int = 512) -> tuple[int, int]:
    """Return a bounded file count/signature for silent build activity.

    This intentionally observes metadata only.  It never reads task contents
    and skips common dependency trees so a large workspace cannot turn the
    watchdog into a second workload.
    """

    count = 0
    signature = 0
    if not root.is_dir():
        return count, signature
    skipped = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
    try:
        for directory, names, files in os.walk(root):
            names[:] = [name for name in names if name not in skipped]
            for name in files:
                try:
                    stat = (Path(directory) / name).stat()
                except OSError:
                    continue
                count += 1
                signature = (signature + stat.st_size + stat.st_mtime_ns) & ((1 << 64) - 1)
                if count >= limit:
                    return count, signature
    except OSError:
        return count, signature
    return count, signature


def _linux_process_tree(pid: int) -> tuple[int, ...]:
    """Best-effort recursive child discovery without a psutil dependency."""

    if os.name != "posix":
        return (pid,)
    pending = [pid]
    found: set[int] = set()
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        try:
            raw = Path(f"/proc/{current}/task/{current}/children").read_text()
        except OSError:
            continue
        for value in raw.split():
            try:
                child = int(value)
            except ValueError:
                continue
            if child not in found:
                pending.append(child)
    return tuple(sorted(found)) or (pid,)


def _windows_process_tree(pid: int) -> tuple[int, ...]:
    """Best-effort Windows descendant discovery when psutil is available."""

    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return (pid,)
    try:
        process = psutil.Process(pid)
        descendants = process.children(recursive=True)
        return tuple(sorted({pid, *(child.pid for child in descendants)}))
    except (OSError, psutil.Error):
        return (pid,)


def _process_rss_bytes(pid: int) -> int:
    """Return a best-effort resident-set size for one process."""

    try:
        pid_value = int(pid)
    except (TypeError, ValueError):
        return 0
    if pid_value < 1:
        return 0
    if os.name == "posix":
        try:
            for line in Path(f"/proc/{pid_value}/status").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.startswith("VmRSS:"):
                    value = line.split()[1]
                    return max(0, int(value) * 1024)
        except (OSError, IndexError, ValueError):
            return 0
        return 0
    try:
        import psutil  # type: ignore[import-not-found]

        return max(0, int(psutil.Process(pid_value).memory_info().rss))
    except (ImportError, OSError, ValueError):
        return 0
    except Exception:
        # psutil may raise NoSuchProcess/ZombieProcess between the liveness
        # check and memory read.  Resource telemetry is best-effort and must
        # never turn a successful command into an executor failure.
        return 0


def _record_activity_snapshot(
    activity: dict[str, Any], snapshot: Mapping[str, int], *, now: float | None = None
) -> None:
    """Update liveness and peak resource counters from one snapshot."""

    current = time.monotonic() if now is None else now
    if _activity_changed(activity.get("snapshot"), snapshot):
        activity["last_activity"] = current
        activity["activity_events"] = int(activity.get("activity_events", 0)) + 1
    activity["snapshot"] = dict(snapshot)
    activity["peak_rss_bytes"] = max(
        int(activity.get("peak_rss_bytes", 0) or 0),
        int(snapshot.get("rss_bytes", 0) or 0),
    )


def _resource_metadata(
    activity: Mapping[str, Any],
    *,
    returncode: int | None = None,
    output: str = "",
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Build bounded, provider-neutral process resource telemetry."""

    snapshot = dict(activity.get("snapshot") or {})
    rss = int(snapshot.get("rss_bytes", 0) or 0)
    peak = max(int(activity.get("peak_rss_bytes", 0) or 0), rss)
    disk_free = int(snapshot.get("disk_free_bytes", 0) or 0)
    if not disk_free and workspace is not None:
        try:
            disk_free = max(0, int(shutil.disk_usage(workspace).free))
        except OSError:
            disk_free = 0
    normalized = str(output or "").casefold()
    oom = bool(
        returncode in {-9, 137}
        or any(marker in normalized for marker in ("out of memory", "oom-kill", "cannot allocate memory"))
    )
    return {
        "rss_bytes": rss,
        "peak_rss_bytes": peak,
        "disk_free_bytes": disk_free,
        "cpu_ticks": int(snapshot.get("cpu_ticks", 0) or 0),
        "io_bytes": int(snapshot.get("io_bytes", 0) or 0),
        "children": int(snapshot.get("children", 0) or 0),
        "network_sockets": int(snapshot.get("network_sockets", 0) or 0),
        "oom_killed": oom,
        "exit_signal": abs(returncode) if isinstance(returncode, int) and returncode < 0 else None,
    }


def _windows_process_metrics(pid: int) -> tuple[int, int]:
    """Read cumulative CPU and transfer bytes without a new dependency."""

    try:
        import ctypes

        class _FileTime(ctypes.Structure):
            _fields_ = [
                ("low", ctypes.c_uint32),
                ("high", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("read_operations", ctypes.c_uint64),
                ("write_operations", ctypes.c_uint64),
                ("other_operations", ctypes.c_uint64),
                ("read_bytes", ctypes.c_uint64),
                ("write_bytes", ctypes.c_uint64),
                ("other_bytes", ctypes.c_uint64),
            ]

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel.GetProcessIoCounters.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_IoCounters),
        ]
        handle = kernel.OpenProcess(0x1000 | 0x0400, 0, pid)
        if not handle:
            return 0, 0
        try:
            creation = _FileTime()
            exit_time = _FileTime()
            kernel_time = _FileTime()
            user_time = _FileTime()
            cpu_ticks = 0
            if kernel.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                kernel_ticks = (kernel_time.high << 32) | kernel_time.low
                user_ticks = (user_time.high << 32) | user_time.low
                cpu_ticks = kernel_ticks + user_ticks
            counters = _IoCounters()
            io_bytes = 0
            if kernel.GetProcessIoCounters(handle, ctypes.byref(counters)):
                io_bytes = counters.read_bytes + counters.write_bytes
            return int(cpu_ticks), int(io_bytes)
        finally:
            kernel.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0, 0


def _process_activity_snapshot(pid: int, workspace: Path) -> dict[str, int]:
    """Collect liveness signals for a process and its descendants.

    CPU ticks, process I/O, descendant count, open sockets and workspace
    metadata are deliberately coarse.  A changed signal resets the idle
    budget; only a live process with no signal change can be idle-timed out.
    """

    pids = _linux_process_tree(pid) if os.name == "posix" else _windows_process_tree(pid)
    cpu_ticks = 0
    io_bytes = 0
    rss_bytes = 0
    sockets = 0
    for current in pids:
        rss_bytes += _process_rss_bytes(current)
        if os.name == "posix":
            try:
                fields = Path(f"/proc/{current}/stat").read_text().split()
                cpu_ticks += int(fields[13]) + int(fields[14])
            except (OSError, IndexError, ValueError):
                pass
            try:
                for line in Path(f"/proc/{current}/io").read_text().splitlines():
                    key, _, value = line.partition(":")
                    if key in {"read_bytes", "write_bytes", "rchar", "wchar"}:
                        io_bytes += int(value.strip())
            except (OSError, ValueError):
                pass
            try:
                sockets += sum(
                    1
                    for fd in Path(f"/proc/{current}/fd").iterdir()
                    if "socket:[" in os.readlink(fd)
                )
            except OSError:
                pass
        else:
            process_cpu, process_io = _windows_process_metrics(current)
            cpu_ticks += process_cpu
            io_bytes += process_io
            try:
                import psutil  # type: ignore[import-not-found]
            except ImportError:
                pass
            else:
                try:
                    sockets += len(psutil.Process(current).net_connections(kind="inet"))
                except (OSError, ValueError, psutil.Error):
                    pass
    file_count, file_signature = _workspace_activity(workspace)
    try:
        disk_free_bytes = max(0, int(shutil.disk_usage(workspace).free))
    except OSError:
        disk_free_bytes = 0
    return {
        "children": len(pids),
        "cpu_ticks": cpu_ticks,
        "io_bytes": io_bytes,
        "rss_bytes": rss_bytes,
        "disk_free_bytes": disk_free_bytes,
        "network_sockets": sockets,
        "workspace_files": file_count,
        "workspace_signature": file_signature,
    }


async def _async_process_activity_snapshot(pid: int, workspace: Path) -> dict[str, int]:
    """Collect process activity without blocking the Runtime event loop.

    Process-tree and ``/proc`` inspection is synchronous for portability, but
    a large fan-out of descendants can make the scan exceed the supervisor
    heartbeat window.  Running it in a worker thread keeps command execution,
    leases, and supervisor ticks responsive while preserving the same snapshot
    schema and timeout semantics.
    """

    return await asyncio.to_thread(_process_activity_snapshot, pid, workspace)


def _activity_changed(previous: Mapping[str, int] | None, current: Mapping[str, int]) -> bool:
    if previous is None:
        return False
    return any(previous.get(key) != current.get(key) for key in current)


def _append_progress(
    path: Path,
    *,
    event: str,
    command_digest: str,
    pid: int,
    elapsed_s: float,
    returncode: int | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    idle_s: float | None = None,
    activity: Mapping[str, Any] | None = None,
) -> None:
    """Append bounded command-liveness evidence without exposing command text."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "command_digest": command_digest,
            "pid": pid,
            "elapsed_s": round(max(0.0, elapsed_s), 3),
            "returncode": returncode,
            "stdout_bytes": max(0, stdout_bytes),
            "stderr_bytes": max(0, stderr_bytes),
            "idle_s": round(max(0.0, idle_s), 3) if idle_s is not None else None,
        }
        if activity:
            payload.update(
                {
                    "activity_events": int(activity.get("activity_events", 0)),
                    "last_activity_s": round(
                        max(
                            0.0,
                            float(
                                idle_s
                                if idle_s is not None
                                else activity.get("last_activity_s", 0)
                            ),
                        ),
                        3,
                    ),
                    "activity": dict(activity.get("snapshot") or {}),
                }
            )
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        # Progress evidence is best-effort and must never change command
        # execution semantics when a log mount is unavailable.
        return


async def _progress_heartbeat(
    path: Path | None,
    proc: asyncio.subprocess.Process,
    *,
    command_digest: str,
    started: float,
    interval_s: float,
    activity: dict[str, Any],
    workspace: Path,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        if proc.returncode is not None:
            return
        now = asyncio.get_running_loop().time()
        snapshot = await _async_process_activity_snapshot(proc.pid, workspace)
        _record_activity_snapshot(activity, snapshot, now=now)
        if path is not None:
            _append_progress(
                path,
                event="heartbeat",
                command_digest=command_digest,
                pid=proc.pid,
                elapsed_s=now - started,
                stdout_bytes=int(activity["stdout_bytes"]),
                stderr_bytes=int(activity["stderr_bytes"]),
                idle_s=(now - float(activity["last_activity"])),
                activity=activity,
            )


async def _drain_pipe(
    stream: asyncio.StreamReader | None,
    chunks: list[bytes],
    *,
    activity: dict[str, Any],
    byte_key: str,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return
        chunks.append(chunk)
        activity[byte_key] = int(activity[byte_key]) + len(chunk)
        now = asyncio.get_running_loop().time()
        activity["last_output"] = now
        activity["last_activity"] = now


async def _collect_process_output(
    proc: asyncio.subprocess.Process,
    *,
    activity: dict[str, Any],
) -> tuple[bytes, bytes]:
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    await asyncio.gather(
        _drain_pipe(proc.stdout, stdout_chunks, activity=activity, byte_key="stdout_bytes"),
        _drain_pipe(proc.stderr, stderr_chunks, activity=activity, byte_key="stderr_bytes"),
        proc.wait(),
    )
    return b"".join(stdout_chunks), b"".join(stderr_chunks)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a timed-out shell and all descendants without leaking pipes."""

    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


@dataclass(frozen=True)
class ShellProfile:
    name: str
    dialect: str
    platform: str
    executable: str
    fallback_encodings: tuple[str, ...]

    def argv(self, command: str) -> list[str]:
        if self.dialect == "powershell":
            utf8_prefix = (
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$PSDefaultParameterValues['Get-Content:Encoding'] = 'utf8'; "
            )
            return [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                utf8_prefix + command,
            ]
        if self.dialect == "cmd":
            return [self.executable, "/d", "/s", "/c", command]
        return [self.executable, "-c", command]


@dataclass
class _BackgroundProcess:
    process: subprocess.Popen
    command_digest: str
    started: float
    started_epoch: float
    start_token: str | None
    stdout_path: Path
    stderr_path: Path
    progress_path: Path | None
    manifest_path: Path
    owner_id: str
    heartbeat_interval: float
    activity: dict[str, Any]
    readiness: dict[str, Any] = field(default_factory=dict)
    service_name: str | None = None
    health_command: str | None = None
    health_timeout_s: float = 2.0
    shutdown_timeout_s: float = 5.0
    heartbeat_task: asyncio.Task[None] | None = None
    finish_recorded: bool = False


def _select_shell_profile(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ShellProfile:
    platform = platform or os.name
    system_encoding = locale.getencoding()
    if platform == "nt":
        for candidate in ("pwsh", "powershell"):
            executable = which(candidate)
            if executable:
                return ShellProfile(
                    name="PowerShell",
                    dialect="powershell",
                    platform="Windows",
                    executable=executable,
                    fallback_encodings=(system_encoding, "oem", "mbcs"),
                )
        executable = which("cmd") or os.environ.get("COMSPEC", "cmd.exe")
        return ShellProfile(
            name="Command Prompt",
            dialect="cmd",
            platform="Windows",
            executable=executable,
            fallback_encodings=(system_encoding, "oem", "mbcs"),
        )

    executable = which("bash") or which("sh") or "/bin/sh"
    name = "Bash" if Path(executable).name.startswith("bash") else "POSIX shell"
    return ShellProfile(
        name=name,
        dialect="posix",
        platform="POSIX",
        executable=executable,
        fallback_encodings=(system_encoding,),
    )


def _decode_process_output(
    raw: bytes,
    *,
    fallback_encodings: tuple[str, ...] = (),
) -> str:
    if not raw:
        return ""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")

    attempted: set[str] = set()
    for encoding in ("utf-8", *fallback_encodings):
        normalized = encoding.lower()
        if normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def strip_secrets(env: dict[str, str]) -> dict[str, str]:
    """删掉名字含 KEY/TOKEN/SECRET/CREDENTIAL/PASSWORD/API 的变量。"""
    return {k: v for k, v in env.items() if not _SECRET_RE.search(k)}


def strip_harness_runtime_loader(env: dict[str, str]) -> dict[str, str]:
    """Remove private harness runtime settings from child-tool environments.

    The Terminal-Bench ``cc-harness`` wrapper needs the frozen verifier
    libraries to start its own Python interpreter.  If that ``LD_LIBRARY_PATH``
    leaks into a task command, however, the task's native ``bash``/``git`` and
    other system binaries try to load the bundled glibc.  The bundled glibc is
    intentionally built for the verifier runtime and is not ABI-compatible
    with every task image, producing errors such as ``__tunable_is_initialized``
    before the command body can run.  Keep unrelated user library paths while
    removing only the harness-owned entries.  Both the old verifier overlay
    and the official custom-agent overlay use the same frozen interpreter, so
    the isolation must apply to either runtime mode.  ``PYTHONHOME`` and
    ``PYTHONPATH`` need the same treatment as ``LD_LIBRARY_PATH``: otherwise a
    task command that invokes its system Python can accidentally import the
    harness interpreter or packages.
    """

    if not any(
        env.get(name) == "1"
        for name in (
            "CC_HARNESS_TERMINAL_VERIFIER_RUNTIME",
            "CC_HARNESS_TERMINAL_AGENT_RUNTIME",
        )
    ):
        return env
    harness_roots = (
        "/opt/cc-harness/verifier-runtime",
        "/opt/cc-harness/agent-runtime",
        "/opt/cc-harness/agent-site",
        "/tmp/cc-lib",
    )

    def harness_owned(path: str) -> bool:
        normalized = path.rstrip("/")
        return any(
            normalized == root or normalized.startswith(root + "/")
            for root in harness_roots
        )

    python_home = env.get("PYTHONHOME")
    if python_home and harness_owned(python_home):
        env.pop("PYTHONHOME", None)

    for variable in ("LD_LIBRARY_PATH", "PYTHONPATH"):
        value = env.get(variable)
        if not value:
            continue
        kept = [part for part in value.split(os.pathsep) if part and not harness_owned(part)]
        if kept:
            env[variable] = os.pathsep.join(kept)
        else:
            env.pop(variable, None)

    # Retain compatibility with early overlay builds that used these exact
    # paths before the runtime roots were standardized.
    legacy_paths = {
        "/opt/cc-harness/verifier-runtime/lib",
        "/tmp/cc-lib",
    }
    value = env.get("LD_LIBRARY_PATH")
    if value:
        kept = [part for part in value.split(os.pathsep) if part not in legacy_paths]
        if kept:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(kept)
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


class Executor(Protocol):
    async def run(self, args: dict, *, cwd: Path) -> ToolResult: ...


class NativeExecutor:
    """asyncio subprocess + cwd 锁 + env 剥离 + 超时。"""

    def __init__(
        self,
        project_root: Path,
        timeout_s: int | float | str | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.timeout_s = resolve_run_command_timeout(timeout_s)
        self.shell_profile = _select_shell_profile()
        self._background_processes: dict[int, _BackgroundProcess] = {}
        # One executor owns the manifests it creates.  The manifest itself is
        # intentionally project-scoped so a newly attached supervisor can
        # inspect an active process without sharing Python memory.
        self._background_owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"

    def _build_env(self) -> dict[str, str]:
        return strip_harness_runtime_loader(strip_secrets(dict(os.environ)))

    @staticmethod
    def _log_size(path: Path) -> int:
        try:
            return max(0, path.stat().st_size)
        except OSError:
            return 0

    @staticmethod
    def _write_background_manifest(path: Path, payload: Mapping[str, Any]) -> None:
        """Atomically persist a bounded background-process manifest."""

        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_background_manifest(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(value) if isinstance(value, Mapping) else None

    @staticmethod
    def _same_process_identity(record: Mapping[str, Any]) -> bool:
        pid = record.get("pid")
        try:
            pid_value = int(pid)
        except (TypeError, ValueError):
            return False
        if not _process_alive(pid_value):
            return False
        expected = record.get("start_token")
        current = _process_start_token(pid_value)
        # If both sides expose a token, equality is mandatory.  A missing
        # token is an explicit best-effort downgrade for minimal Windows hosts.
        return not expected or not current or str(expected) == str(current)

    def _background_manifest_payload(
        self,
        entry: _BackgroundProcess,
        *,
        state: str,
        exit_code: int | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": BACKGROUND_PROCESS_SCHEMA,
            "pid": entry.process.pid,
            "command_digest": entry.command_digest,
            "stdout_log": str(entry.stdout_path),
            "stderr_log": str(entry.stderr_path),
            "progress_file": str(entry.progress_path) if entry.progress_path else None,
            "started_at": datetime.fromtimestamp(entry.started_epoch, UTC).isoformat(),
            "started_epoch": entry.started_epoch,
            "start_token": entry.start_token,
            "owner_id": entry.owner_id,
            "state": state,
            "exit_code": exit_code,
            "readiness": dict(entry.readiness),
            "service_name": entry.service_name,
            "health_command": entry.health_command,
            "health_timeout_s": entry.health_timeout_s,
            "shutdown_timeout_s": entry.shutdown_timeout_s,
            "activity": dict(entry.activity.get("snapshot") or {}),
            "resource": _resource_metadata(
                entry.activity,
                returncode=exit_code,
                workspace=self.project_root,
            ),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def _background_metadata(self, entry: _BackgroundProcess) -> dict[str, Any]:
        returncode = entry.process.poll()
        state = "running" if returncode is None else "exited"
        # Activity is sampled by ``_background_heartbeat``.  Do not perform a
        # synchronous process-tree scan from this status path: callers invoke
        # ``background_status`` on the Runtime event loop, and a large child
        # fan-out could otherwise starve supervisor ticks.  The latest cached
        # snapshot is sufficient for status reporting and remains available
        # after the process exits.
        snapshot = dict(entry.activity.get("snapshot") or {})
        return {
            "background": True,
            "background_supported": True,
            "state": state,
            "pid": entry.process.pid,
            "exit_code": returncode,
            "timed_out": False,
            "idle_timed_out": False,
            "command_digest": entry.command_digest,
            "stdout_log": str(entry.stdout_path),
            "stderr_log": str(entry.stderr_path),
            "progress_file": str(entry.progress_path) if entry.progress_path else None,
            "stdout_bytes": self._log_size(entry.stdout_path),
            "stderr_bytes": self._log_size(entry.stderr_path),
            "activity_events": int(entry.activity.get("activity_events", 0)),
            "activity": snapshot,
            "resource": _resource_metadata(
                entry.activity,
                returncode=returncode,
                workspace=self.project_root,
            ),
            "started_at_monotonic": entry.started,
            "started_at": datetime.fromtimestamp(entry.started_epoch, UTC).isoformat(),
            "readiness": dict(entry.readiness),
            "service_name": entry.service_name,
            "health_command": entry.health_command,
            "health_timeout_s": entry.health_timeout_s,
        }

    def _background_metadata_from_manifest(
        self,
        record: Mapping[str, Any],
        path: Path,
    ) -> dict[str, Any] | None:
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        if not self._same_process_identity(record):
            # The manifest is retained as historical evidence once a process
            # has exited, but a stale PID must never be reported as running.
            if str(record.get("state")) == "running":
                updated = dict(record)
                updated.update(
                    {
                        "state": "exited",
                        "exit_code": updated.get("exit_code"),
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
                self._write_background_manifest(path, updated)
            if str(record.get("state")) == "running":
                return None
        state = "running" if self._same_process_identity(record) else str(record.get("state") or "exited")
        stdout_path = Path(str(record.get("stdout_log") or ""))
        stderr_path = Path(str(record.get("stderr_log") or ""))
        progress_file = record.get("progress_file")
        progress_path = Path(str(progress_file)) if progress_file else None
        return {
            "background": True,
            "background_supported": True,
            "state": state,
            "pid": pid,
            "exit_code": record.get("exit_code"),
            "timed_out": False,
            "idle_timed_out": False,
            "command_digest": str(record.get("command_digest") or ""),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "progress_file": str(progress_path) if progress_path else None,
            "stdout_bytes": self._log_size(stdout_path),
            "stderr_bytes": self._log_size(stderr_path),
            "activity_events": 0,
            "activity": dict(record.get("activity") or {}),
            "resource": dict(record.get("resource") or {}),
            "started_at": record.get("started_at"),
            "readiness": dict(record.get("readiness") or {}),
            "service_name": record.get("service_name"),
            "health_command": record.get("health_command"),
            "health_timeout_s": float(record.get("health_timeout_s") or 2.0),
            "shutdown_timeout_s": float(record.get("shutdown_timeout_s") or 5.0),
            "manifest": str(path),
            "owner_id": record.get("owner_id"),
        }

    async def _background_heartbeat(self, entry: _BackgroundProcess) -> None:
        """Persist liveness for a detached command without imposing idle timeout."""

        try:
            while entry.process.poll() is None:
                await asyncio.sleep(entry.heartbeat_interval)
                if entry.process.poll() is not None:
                    break
                now = time.monotonic()
                snapshot = await _async_process_activity_snapshot(
                    entry.process.pid, self.project_root
                )
                _record_activity_snapshot(entry.activity, snapshot, now=now)
                if entry.progress_path is not None:
                    _append_progress(
                        entry.progress_path,
                        event="background_heartbeat",
                        command_digest=entry.command_digest,
                        pid=entry.process.pid,
                        elapsed_s=now - entry.started,
                        stdout_bytes=self._log_size(entry.stdout_path),
                        stderr_bytes=self._log_size(entry.stderr_path),
                        idle_s=now - float(entry.activity.get("last_activity", now)),
                        activity=entry.activity,
                    )
        finally:
            # Cancellation is the normal shutdown path.  Do not publish an
            # exited event while the process is still alive; the owner records
            # the final return code after its process-group termination.
            if entry.process.poll() is not None:
                self._record_background_finish(entry)

    def _record_background_finish(self, entry: _BackgroundProcess) -> None:
        if entry.finish_recorded:
            # Even when no progress file was requested, retain the process
            # manifest so a resumed supervisor can distinguish an exited
            # service from an unknown PID.
            return
        returncode = entry.process.poll()
        if returncode is None:
            return
        now = time.monotonic()
        if entry.progress_path is not None:
            _append_progress(
                entry.progress_path,
                event="background_finish",
                command_digest=entry.command_digest,
                pid=entry.process.pid,
                elapsed_s=now - entry.started,
                returncode=returncode,
                stdout_bytes=self._log_size(entry.stdout_path),
                stderr_bytes=self._log_size(entry.stderr_path),
                idle_s=now - float(entry.activity.get("last_activity", now)),
                activity=entry.activity,
            )
        self._write_background_manifest(
            entry.manifest_path,
            self._background_manifest_payload(
                entry,
                state="exited",
                exit_code=returncode,
            ),
        )
        entry.finish_recorded = True

    async def _run_background(
        self,
        command: str,
        *,
        command_digest: str,
        progress_path: Path | None,
        readiness_command: str | None = None,
        readiness_timeout_s: float | None = None,
        service_name: str | None = None,
        health_command: str | None = None,
        health_timeout_s: float | None = None,
        shutdown_timeout_s: float | None = None,
    ) -> ToolResult:
        """Start an explicitly requested long-lived process and return its handle."""

        process_dir = self.project_root / ".cc-harness" / "processes"
        stamp = f"{time.time_ns()}-{command_digest[7:19]}"
        stdout_path = process_dir / f"{stamp}.stdout.log"
        stderr_path = process_dir / f"{stamp}.stderr.log"
        manifest_path = process_dir / f"{stamp}.json"
        stdout_file = None
        stderr_file = None
        try:
            process_dir.mkdir(parents=True, exist_ok=True)
            stdout_file = stdout_path.open("ab")
            stderr_file = stderr_path.open("ab")
            process_options: dict[str, object] = {}
            if os.name == "posix":
                process_options["start_new_session"] = True
            elif os.name == "nt":
                process_options["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            process = subprocess.Popen(
                self.shell_profile.argv(command),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=str(self.project_root),
                env=self._build_env(),
                close_fds=os.name != "nt",
                **process_options,
            )
        except Exception as exc:
            for handle in (stdout_file, stderr_file):
                if handle is not None:
                    handle.close()
            return ToolResult.error(
                display=f"background process failed to start: {exc}",
                llm=f"[Tool Error] background process failed to start: {type(exc).__name__}: {exc}",
                metadata={
                    "background": True,
                    "background_supported": True,
                    "state": "failed",
                    "exit_code": None,
                    "exception": type(exc).__name__,
                    "command_digest": command_digest,
                    "stdout_log": str(stdout_path),
                    "stderr_log": str(stderr_path),
                    "progress_file": str(progress_path) if progress_path else None,
                    "manifest": str(manifest_path),
                },
            )
        finally:
            # Popen owns duplicated descriptors; the parent must not hold them.
            for handle in (stdout_file, stderr_file):
                if handle is not None and not handle.closed:
                    handle.close()

        started = time.monotonic()
        started_epoch = time.time()
        entry = _BackgroundProcess(
            process=process,
            command_digest=command_digest,
            started=started,
            started_epoch=started_epoch,
            start_token=_process_start_token(process.pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            progress_path=progress_path,
            manifest_path=manifest_path,
            owner_id=self._background_owner_id,
            heartbeat_interval=_resolve_progress_heartbeat(os.getenv(RUN_COMMAND_HEARTBEAT_ENV)),
            activity={
                "last_output": started,
                "last_activity": started,
                "activity_events": 0,
                "snapshot": await _async_process_activity_snapshot(
                    process.pid, self.project_root
                ),
                "peak_rss_bytes": 0,
            },
            service_name=service_name.strip() if isinstance(service_name, str) and service_name.strip() else None,
            health_command=health_command.strip() if isinstance(health_command, str) and health_command.strip() else None,
            health_timeout_s=min(30.0, max(0.1, float(health_timeout_s or 2.0))),
            shutdown_timeout_s=min(30.0, max(0.1, float(shutdown_timeout_s or 5.0))),
        )
        if readiness_command is not None:
            entry.readiness = {
                "requested": True,
                "command_digest": _command_digest(readiness_command),
                "status": "checking",
                "timeout_s": _resolve_background_readiness_timeout(readiness_timeout_s),
            }
        else:
            entry.readiness = {"requested": False, "status": "not_requested"}
        self._background_processes[process.pid] = entry
        self._write_background_manifest(
            manifest_path,
            self._background_manifest_payload(entry, state="running", exit_code=None),
        )
        if progress_path is not None:
            _append_progress(
                progress_path,
                event="background_start",
                command_digest=command_digest,
                pid=process.pid,
                elapsed_s=0,
                stdout_bytes=0,
                stderr_bytes=0,
                idle_s=0,
                activity=entry.activity,
            )
        entry.heartbeat_task = asyncio.create_task(self._background_heartbeat(entry))
        if readiness_command is not None:
            await self._wait_for_background_readiness(
                entry,
                readiness_command,
                _resolve_background_readiness_timeout(readiness_timeout_s),
            )
            self._write_background_manifest(
                manifest_path,
                self._background_manifest_payload(
                    entry,
                    state="running" if process.poll() is None else "exited",
                    exit_code=process.poll(),
                ),
            )
        metadata = self._background_metadata(entry)
        metadata["manifest"] = str(manifest_path)
        return ToolResult.success(
            (
                f"background process started (pid={process.pid}); "
                f"stdout: {stdout_path}; stderr: {stderr_path}; "
                f"readiness: {entry.readiness.get('status')}"
            ),
            metadata=metadata,
        )

    async def _wait_for_background_readiness(
        self,
        entry: _BackgroundProcess,
        readiness_command: str,
        timeout_s: float,
    ) -> None:
        """Poll an optional health command without converting startup delay to failure."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        last_exit: int | None = None
        last_output = ""
        while entry.process.poll() is None and asyncio.get_running_loop().time() < deadline:
            remaining = max(0.05, deadline - asyncio.get_running_loop().time())
            probe_timeout = min(2.0, remaining)
            probe = None
            try:
                probe = await asyncio.create_subprocess_exec(
                    *self.shell_profile.argv(readiness_command),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.project_root),
                    env=self._build_env(),
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    probe.communicate(), timeout=probe_timeout
                )
                last_exit = probe.returncode
                last_output = (_decode_process_output(stdout_b) + "\n" + _decode_process_output(stderr_b)).strip()[-512:]
            except asyncio.TimeoutError:
                if probe is not None:
                    await _terminate_process_tree(probe)
                last_exit = None
                last_output = "readiness probe timed out"
            except (OSError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                last_exit = None
                last_output = f"{type(exc).__name__}: {exc}"[-512:]
            if last_exit == 0:
                entry.readiness.update(
                    {"status": "ready", "exit_code": 0, "checked_at": datetime.now(UTC).isoformat()}
                )
                return
            await asyncio.sleep(min(0.25, max(0.05, deadline - asyncio.get_running_loop().time())))
        if entry.process.poll() is not None:
            entry.readiness.update(
                {
                    "status": "process_exited",
                    "exit_code": entry.process.poll(),
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            )
        else:
            entry.readiness.update(
                {
                    "status": "timeout",
                    "exit_code": last_exit,
                    "detail": last_output,
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            )

    async def start_managed_service(
        self,
        contract: ManagedServiceContract,
        *,
        progress_path: Path | None = None,
    ) -> ToolResult:
        """Start a service under the same durable process contract as commands."""

        result = await self._run_background(
            contract.command,
            command_digest=_command_digest(contract.command),
            progress_path=progress_path,
            readiness_command=contract.readiness_command,
            readiness_timeout_s=contract.readiness_timeout_s,
            service_name=contract.name,
            health_command=contract.health_command,
            health_timeout_s=contract.health_timeout_s,
            shutdown_timeout_s=contract.shutdown_timeout_s,
        )
        if result.is_error:
            return result
        metadata = dict(result.metadata)
        metadata["service_contract"] = contract.to_dict()
        return ToolResult.success(result.llm_text, metadata=metadata)

    async def managed_service_status(
        self,
        pid: int,
        *,
        probe: bool = True,
    ) -> dict[str, Any] | None:
        """Return process and health state, never conflating liveness with readiness."""

        status = self.background_status(pid)
        if status is None:
            return None
        health_command = status.get("health_command")
        if not probe or not health_command or status.get("state") != "running":
            status.setdefault("health", {"status": "not_configured" if not health_command else "unknown"})
            return status
        timeout = min(30.0, max(0.1, float(status.get("health_timeout_s") or 2.0)))
        probe_process = None
        try:
            probe_process = await asyncio.create_subprocess_exec(
                *self.shell_profile.argv(str(health_command)),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_root),
                env=self._build_env(),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                probe_process.communicate(), timeout=timeout
            )
            combined = (
                _decode_process_output(stdout_b, fallback_encodings=self.shell_profile.fallback_encodings)
                + "\n"
                + _decode_process_output(stderr_b, fallback_encodings=self.shell_profile.fallback_encodings)
            ).strip()
            status["health"] = {
                "status": "healthy" if probe_process.returncode == 0 else "unhealthy",
                "exit_code": probe_process.returncode,
                "checked_at": datetime.now(UTC).isoformat(),
                "detail": combined[-512:],
            }
        except asyncio.TimeoutError:
            if probe_process is not None:
                await _terminate_process_tree(probe_process)
            status["health"] = {
                "status": "timeout",
                "exit_code": None,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        except (OSError, ValueError) as exc:
            status["health"] = {
                "status": "probe_error",
                "exit_code": None,
                "checked_at": datetime.now(UTC).isoformat(),
                "detail": f"{type(exc).__name__}: {exc}"[-512:],
            }
        return status

    async def stop_managed_service(self, pid: int) -> bool:
        return await self.stop_background(pid)

    def background_status(self, pid: int) -> dict[str, Any] | None:
        entry = self._background_processes.get(int(pid))
        if entry is not None:
            metadata = self._background_metadata(entry)
            self._write_background_manifest(
                entry.manifest_path,
                self._background_manifest_payload(
                    entry,
                    state=metadata["state"],
                    exit_code=metadata["exit_code"],
                ),
            )
            metadata["manifest"] = str(entry.manifest_path)
            return metadata
        process_dir = self.project_root / ".cc-harness" / "processes"
        if not process_dir.is_dir():
            return None
        try:
            manifests = sorted(
                process_dir.glob("*.json"),
                key=lambda path: (path.stat().st_mtime_ns, str(path)),
                reverse=True,
            )
        except OSError:
            manifests = []
        for path in manifests:
            record = self._read_background_manifest(path)
            if (
                record is None
                or record.get("schema_version") != BACKGROUND_PROCESS_SCHEMA
                or record.get("pid") != int(pid)
            ):
                continue
            return self._background_metadata_from_manifest(record, path)
        return None

    async def stop_background(self, pid: int) -> bool:
        """Stop one owned process using its persisted identity-safe manifest."""

        pid = int(pid)
        entry = self._background_processes.get(pid)
        if entry is not None:
            await self._terminate_background_process(entry)
            self._background_processes.pop(pid, None)
            try:
                entry.manifest_path.unlink(missing_ok=True)
            except OSError:
                pass
            return True
        process_dir = self.project_root / ".cc-harness" / "processes"
        for path in process_dir.glob("*.json") if process_dir.is_dir() else ():
            record = self._read_background_manifest(path)
            if (
                record is None
                or record.get("schema_version") != BACKGROUND_PROCESS_SCHEMA
                or record.get("pid") != pid
            ):
                continue
            if not self._same_process_identity(record):
                return False
            try:
                if os.name == "posix":
                    os.killpg(pid, signal.SIGTERM)
                else:
                    await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                        capture_output=True,
                        timeout=5,
                    )
            except (OSError, subprocess.TimeoutExpired):
                return False
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return True
        return False

    async def _terminate_background_process(self, entry: _BackgroundProcess) -> None:
        if entry.heartbeat_task is not None and not entry.heartbeat_task.done():
            entry.heartbeat_task.cancel()
            await asyncio.gather(entry.heartbeat_task, return_exceptions=True)
        process = entry.process
        if process.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(process.wait, 1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            else:
                try:
                    await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        capture_output=True,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            try:
                await asyncio.to_thread(process.wait, 5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        self._record_background_finish(entry)

    async def kill(self) -> bool:
        """Stop all explicitly detached commands owned by this executor."""

        entries = list(self._background_processes.values())
        for entry in entries:
            await self._terminate_background_process(entry)
            self._background_processes.pop(entry.process.pid, None)
            try:
                # An explicit executor shutdown is an ownership boundary, not
                # historical evidence.  Remove the live manifest so a later
                # status query cannot mistake a deliberately stopped service
                # for a still-owned process.
                entry.manifest_path.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    async def run(self, args: dict, *, cwd: Path) -> ToolResult:
        command = args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                display="'command' must be a non-empty string",
                llm="[Tool Error] 'command' must be a non-empty string",
            )
        command_digest = _command_digest(command)
        deadline_budget = remaining_task_budget()
        command_timeout = (
            min(self.timeout_s, deadline_budget)
            if deadline_budget is not None
            else self.timeout_s
        )
        progress_raw = os.getenv(RUN_COMMAND_PROGRESS_FILE_ENV)
        progress_path = Path(progress_raw) if progress_raw else None
        # Keep failure telemetry well-formed even when process creation itself
        # fails (missing shell, invalid cwd, or an OS-level spawn error).
        # Previously the exception handler referenced ``activity`` before it
        # had been initialized, masking the real infrastructure error.
        activity: dict[str, Any] = {
            "last_output": 0.0,
            "last_activity": 0.0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "activity_events": 0,
            "snapshot": {},
            "peak_rss_bytes": 0,
        }
        if bool(args.get("background", False)):
            return await self._run_background(
                command,
                command_digest=command_digest,
                progress_path=progress_path,
                readiness_command=(
                    str(args.get("readiness_command"))
                    if isinstance(args.get("readiness_command"), str)
                    and args.get("readiness_command").strip()
                    else None
                ),
                readiness_timeout_s=args.get("readiness_timeout_s"),
                service_name=(
                    str(args.get("service_name"))
                    if isinstance(args.get("service_name"), str)
                    and args.get("service_name").strip()
                    else None
                ),
                health_command=(
                    str(args.get("health_command"))
                    if isinstance(args.get("health_command"), str)
                    and args.get("health_command").strip()
                    else None
                ),
                health_timeout_s=args.get("health_timeout_s"),
                shutdown_timeout_s=args.get("shutdown_timeout_s"),
            )
        try:
            process_options: dict[str, object] = {}
            if os.name == "posix":
                # A command may spawn apt/build children.  Put the shell in
                # its own group so timeout cleanup can kill the full tree.
                process_options["start_new_session"] = True
            elif os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            proc = await asyncio.create_subprocess_exec(
                *self.shell_profile.argv(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_root),  # 锁项目根,忽略传入 cwd
                env=self._build_env(),
                **process_options,
            )
            started = asyncio.get_running_loop().time()
            activity = {
                "last_output": started,
                "last_activity": started,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "activity_events": 0,
                "snapshot": await _async_process_activity_snapshot(
                    proc.pid, self.project_root
                ),
                "peak_rss_bytes": 0,
            }
            idle_timeout = resolve_run_command_idle_timeout()
            heartbeat_interval = _resolve_progress_heartbeat(
                os.getenv(RUN_COMMAND_HEARTBEAT_ENV)
            )
            heartbeat_task: asyncio.Task[None] | None = None
            if progress_path is not None:
                _append_progress(
                    progress_path,
                    event="start",
                    command_digest=command_digest,
                    pid=proc.pid,
                    elapsed_s=0,
                    stdout_bytes=0,
                    stderr_bytes=0,
                    idle_s=0,
                )
            heartbeat_task = asyncio.create_task(
                _progress_heartbeat(
                    progress_path,
                    proc,
                    command_digest=command_digest,
                    started=started,
                    interval_s=heartbeat_interval,
                    activity=activity,
                    workspace=self.project_root,
                )
            )
            collector = asyncio.create_task(
                _collect_process_output(proc, activity=activity)
            )
            stdout_b = b""
            stderr_b = b""
            try:
                while not collector.done():
                    now = asyncio.get_running_loop().time()
                    snapshot = await _async_process_activity_snapshot(
                        proc.pid, self.project_root
                    )
                    _record_activity_snapshot(activity, snapshot, now=now)
                    total_remaining = command_timeout - (now - started)
                    idle_remaining = (
                        idle_timeout - (now - float(activity["last_activity"]))
                        if idle_timeout is not None
                        else total_remaining
                    )
                    if total_remaining <= 0 or idle_remaining <= 0:
                        idle_timed_out = (
                            idle_timeout is not None
                            and idle_remaining <= 0
                            and idle_remaining <= total_remaining
                        )
                        timeout_text = (
                            f"idle timeout after {idle_timeout}s without observable activity"
                            if idle_timed_out
                            else f"timeout after {command_timeout}s"
                        )
                        await _terminate_process_tree(proc)
                        try:
                            stdout_b, stderr_b = await asyncio.wait_for(
                                asyncio.shield(collector), timeout=5
                            )
                        except (asyncio.TimeoutError, asyncio.CancelledError, OSError):
                            collector.cancel()
                            await asyncio.gather(collector, return_exceptions=True)
                        stdout = _decode_process_output(stdout_b)
                        stderr = _decode_process_output(stderr_b)
                        combined = (stdout + "\n" + stderr).strip()
                        return ToolResult.error(
                            display=(
                                f"{timeout_text}: {combined[:200]}"
                                if combined
                                else timeout_text
                            ),
                            llm=(
                                f"[Tool Error] {timeout_text}\n"
                                f"stdout: {stdout}\nstderr: {stderr}"
                            ),
                            metadata={
                                "exit_code": None,
                                "timed_out": True,
                                "idle_timed_out": idle_timed_out,
                                "stdout": stdout,
                                "stderr": stderr,
                                "stdout_chunks": _output_chunks(stdout),
                                "stderr_chunks": _output_chunks(stderr),
                                "command_digest": command_digest,
                                "progress_file": (
                                    str(progress_path) if progress_path else None
                                ),
                                "stdout_bytes": int(activity["stdout_bytes"]),
                                "stderr_bytes": int(activity["stderr_bytes"]),
                                "idle_s": round(
                                    max(0.0, now - float(activity["last_activity"])), 3
                                ),
                                "activity_events": int(activity.get("activity_events", 0)),
                                "activity": dict(activity.get("snapshot") or {}),
                                "resource": _resource_metadata(
                                    activity,
                                    returncode=None,
                                    output=combined,
                                    workspace=self.project_root,
                                ),
                                "task_deadline_remaining_s": deadline_budget,
                            },
                        )
                    wait_seconds = min(
                        heartbeat_interval,
                        total_remaining,
                        idle_remaining,
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(collector), timeout=wait_seconds
                        )
                    except asyncio.TimeoutError:
                        continue
                stdout_b, stderr_b = collector.result()
            finally:
                if not collector.done():
                    collector.cancel()
                    await asyncio.gather(collector, return_exceptions=True)
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                if progress_path is not None:
                    now = asyncio.get_running_loop().time()
                    _append_progress(
                        progress_path,
                        event="finish",
                        command_digest=command_digest,
                        pid=proc.pid,
                        elapsed_s=now - started,
                        returncode=proc.returncode,
                        stdout_bytes=int(activity["stdout_bytes"]),
                        stderr_bytes=int(activity["stderr_bytes"]),
                        idle_s=now - float(activity["last_activity"]),
                        activity=activity,
                    )
        except Exception as e:
            return ToolResult.error(
                display=f"raised: {e}",
                llm=f"[Tool Error] {type(e).__name__}: {e}",
                metadata={
                    "exit_code": None,
                    "timed_out": False,
                    "exception": type(e).__name__,
                    "stdout": "",
                    "stderr": "",
                    "stdout_chunks": [],
                    "stderr_chunks": [],
                    "command_digest": command_digest,
                    "progress_file": str(progress_path) if progress_path else None,
                    "activity_events": int(activity.get("activity_events", 0)),
                    "activity": dict(activity.get("snapshot") or {}),
                    "resource": _resource_metadata(
                        activity,
                        returncode=None,
                        workspace=self.project_root,
                    ),
                    "task_deadline_remaining_s": deadline_budget,
                },
            )

        stdout = _decode_process_output(
            stdout_b, fallback_encodings=self.shell_profile.fallback_encodings
        )
        stderr = _decode_process_output(
            stderr_b, fallback_encodings=self.shell_profile.fallback_encodings
        )
        if proc.returncode != 0:
            combined = (stdout + stderr).strip() or f"(no output, exit {proc.returncode})"
            return ToolResult.error(
                display=f"exit {proc.returncode}: {combined[:200]}",
                llm=f"[Tool Error] exit {proc.returncode}\nstdout: {stdout}\nstderr: {stderr}",
                metadata={
                    "exit_code": proc.returncode,
                    "timed_out": False,
                    "stdout": stdout,
                    "stderr": stderr,
                    "stdout_chunks": _output_chunks(stdout),
                    "stderr_chunks": _output_chunks(stderr),
                    "command_digest": command_digest,
                    "progress_file": str(progress_path) if progress_path else None,
                    "activity_events": int(activity.get("activity_events", 0)),
                    "activity": dict(activity.get("snapshot") or {}),
                    "resource": _resource_metadata(
                        activity,
                        returncode=proc.returncode,
                        output=combined,
                        workspace=self.project_root,
                    ),
                    "task_deadline_remaining_s": deadline_budget,
                },
            )
        return ToolResult.success(
            stdout if stdout else "(no output)",
            metadata={
                "exit_code": 0,
                "timed_out": False,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_chunks": _output_chunks(stdout),
                "stderr_chunks": _output_chunks(stderr),
                "command_digest": command_digest,
                "progress_file": str(progress_path) if progress_path else None,
                "activity_events": int(activity.get("activity_events", 0)),
                "activity": dict(activity.get("snapshot") or {}),
                "resource": _resource_metadata(
                    activity,
                    returncode=0,
                    output=f"{stdout}\n{stderr}",
                    workspace=self.project_root,
                ),
                "task_deadline_remaining_s": deadline_budget,
            },
        )


def build_executor(cfg, project_root: Path) -> Executor:
    """按 ExecutorConfig 选 NativeExecutor / SandboxExecutor。

    cfg.enabled=False 强制 native(紧急 kill-switch / 回退)。
    SandboxExecutor 局部 import:避免模块加载即拉起 opensandbox SDK import 链
    (无 [sandbox] extra 的环境也能 import executor.py)。
    """
    from cc_harness.config import ExecutorBackend
    # Legacy ``enabled=false`` must not silently select host execution.
    if cfg.backend is ExecutorBackend.NATIVE:
        return NativeExecutor(project_root=project_root)
    from cc_harness.sandbox import SandboxExecutor
    return SandboxExecutor(cfg.sandbox, project_root=project_root)
