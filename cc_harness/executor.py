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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    sockets = 0
    for current in pids:
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
    return {
        "children": len(pids),
        "cpu_ticks": cpu_ticks,
        "io_bytes": io_bytes,
        "network_sockets": sockets,
        "workspace_files": file_count,
        "workspace_signature": file_signature,
    }


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
        snapshot = _process_activity_snapshot(proc.pid, workspace)
        previous = activity.get("snapshot")
        if _activity_changed(previous, snapshot):
            activity["last_activity"] = now
            activity["activity_events"] = int(activity.get("activity_events", 0)) + 1
        activity["snapshot"] = snapshot
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

    def _build_env(self) -> dict[str, str]:
        return strip_harness_runtime_loader(strip_secrets(dict(os.environ)))

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
        }
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
                "snapshot": _process_activity_snapshot(proc.pid, self.project_root),
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
                    snapshot = _process_activity_snapshot(proc.pid, self.project_root)
                    if _activity_changed(activity.get("snapshot"), snapshot):
                        activity["last_activity"] = now
                        activity["activity_events"] = int(activity.get("activity_events", 0)) + 1
                    activity["snapshot"] = snapshot
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
