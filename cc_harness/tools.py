"""Dangerous-command detection + user confirmation prompt + built-in tools."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from cc_harness.config import ExecutorBackend, ExecutorConfig
from cc_harness.executor import (
    Executor,
    NativeExecutor,
    build_executor,
    remaining_task_budget,
    _terminate_process_tree,
)
from cc_harness.mcp_client import ToolResult
from cc_harness.network import (
    is_network_operation,
    is_transient_network_failure,
    resolve_network_retry_backoff,
    resolve_network_retry_limit,
)

# 体验级安全 — 不是安全边界。真正安全要靠沙箱和权限控制,这里只是防误操作的提示。
# MVP: 只匹配最危险的 rm -rf(避免 rm -r 这种日常用法频繁误报)。
DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\brm\s+--\s"),
    re.compile(r"\brm\s+.*--no-preserve-root\b"),
    re.compile(r"\bdel\s+/[sqf]\b"),
    re.compile(r"\bformat\s+[a-zA-Z]:"),
    re.compile(r"\bdrop\s+(database|table|schema)\b"),
    re.compile(r"\btruncate\s+table\b"),
    re.compile(r":\(\)\{\s*:\|:&\s*\};:"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
]

_SHELL_TOOL_SUFFIX_RE = re.compile(r"__(bash|run_command|shell|execute)$")

def is_dangerous(tool_name: str, arguments: dict) -> bool:
    """Return True if this tool call matches a known dangerous pattern.

    NOTE: This is NO LONGER a gate. The L4 policy engine (policy.py) decides
    allow/ask; this function is only used to enrich the ask reason (e.g.
    "执行 shell 命令需用户确认(命中危险命令模式)"). Kept because policy.py
    imports it for that purpose, and the existing is_dangerous tests stay.

    Scans only the 'command' field of shell-class tools. write_file content is
    never scanned (see spec § 危险命令匹配).
    """
    is_shell = bool(_SHELL_TOOL_SUFFIX_RE.search(tool_name))
    has_command_field = "command" in arguments
    if not (is_shell or has_command_field):
        return False

    command = arguments.get("command", "")
    if not isinstance(command, str):
        return False

    return any(p.search(command) for p in DANGEROUS_PATTERNS)


def confirm(prompt: str) -> bool:
    """Interactive y/N prompt. Default N (Enter = No)."""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


# 红队 allow 模式短路:env 设 yes/always 时 confirm_tool 不读 stdin 直接返回,
# 让用户同意的命令进沙箱执行(测沙箱隔离,非测 agent 姿态)。未设(deny 模式)→
# 走 input(),wrapper 喂的 "exit" 行被读成非 yes/no → no(命令不执行,现状不变)。
# 仅红队 wrapper 用;生产 REPL 不设此 env,交互确认照常。
AUTOCONFIRM_ENV = "CC_HARNESS_AUTOCONFIRM"


def confirm_tool(tool_name: str, args: dict) -> str:
    """3-way confirmation for the L4 policy gate. Returns 'yes' / 'always' / 'no'.

    Default is 'no' (Enter = no). EOF / Ctrl-C → 'no' (fail-closed).

    红队 allow 模式:CC_HARNESS_AUTOCONFIRM=yes|always 短路(不读 stdin)——
    wrapper 设此 env 让命令进沙箱执行,测沙箱隔离而非 agent 闸门姿态。
    """
    auto = os.getenv(AUTOCONFIRM_ENV, "").strip().lower()
    if auto in ("yes", "always"):
        return auto
    prompt = f"允许执行 {tool_name}?(yes / always / [no])"
    try:
        answer = input(f"{prompt}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "no"
    if answer in ("y", "yes"):
        return "yes"
    if answer in ("a", "always"):
        return "always"
    return "no"


# --- Built-in tools (registered as native functions, not via MCP) ---

# Per-call timeout for run_command. Long enough for most builds/tests,
# short enough to surface hangs fast.
RUN_COMMAND_TIMEOUT_S = 30


# --- Session-level executor singleton (Task 9) ---
# 会话级复用:sandbox 容器跨命令复用,避免每条命令 cold-start。
# repl 启动调 init_session_executor,repl 退出调 shutdown_session_executor。
_session_executor: Executor | None = None
# Retained so status surfaces can report the selected backend.
_session_executor_config: ExecutorConfig | None = None
# Native fallback is deliberately opt-in at the executor seam.  Runtime
# entrypoints enable it for ordinary local work, while hardened/evaluation
# profiles leave it disabled so a missing sandbox cannot produce a false
# isolation result.
_session_allow_native_fallback = False
_session_fallback_profile: str | None = None
_session_fallback_reason: str | None = None
_session_fallback_lock = asyncio.Lock()


class ExecutorNotInitializedError(RuntimeError):
    """Raised when no session executor was explicitly selected."""


def init_session_executor(config: ExecutorConfig, project_root: str | Path) -> None:
    """repl 启动调:按 config.backend 建会话级 executor(native 或 sandbox)。

    project_root 锁执行 cwd;sandbox 在容器内 mount 该根为只读。
    Store config for status and diagnostics.
    """
    global _session_executor, _session_executor_config
    global _session_allow_native_fallback, _session_fallback_profile
    global _session_fallback_reason
    _session_executor = build_executor(config, Path(project_root))
    _session_executor_config = config
    # A process can attach a new project/session without passing through the
    # full shutdown path (the WebUI keeps a manager-level client cache).  Do
    # not let a previous project's fallback reason leak into the new status
    # surface or make a fresh NativeExecutor look like a degraded fallback.
    _session_allow_native_fallback = False
    _session_fallback_profile = None
    _session_fallback_reason = None
    _surface_shell_metadata(_session_executor)


def configure_session_native_fallback(
    enabled: bool,
    *,
    capability_profile: str | None = None,
) -> None:
    """Configure transparent local fallback for this executor session.

    The fallback is only a recovery path for OpenSandbox infrastructure
    outages.  It is disabled for hardened/security profiles, benchmark
    processes, or an explicit ``CC_HARNESS_SANDBOX_FALLBACK=hard`` override.
    ``CC_HARNESS_SANDBOX_FALLBACK=native`` is accepted as an explicit local
    opt-in for callers that do not use a capability profile.
    """

    global _session_allow_native_fallback, _session_fallback_profile
    profile = (capability_profile or "").strip().lower() or None
    security_mode = os.getenv("CC_HARNESS_SECURITY_MODE", "").strip().lower()
    restricted_profile = profile == "hardened-safety" or security_mode in {
        "strict",
        "hardened",
        "security",
    }
    benchmark_process = any(
        os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in (
            "CC_HARNESS_TERMINAL_BENCH",
            "CC_HARNESS_EVAL",
            # The official Harbor adapter always sets this marker.  Treat it
            # as an isolation boundary even if a caller forgot the friendly
            # benchmark alias above.
            "CC_HARNESS_TERMINAL_AGENT_RUNTIME",
        )
    )
    explicit_mode = os.getenv("CC_HARNESS_SANDBOX_FALLBACK", "").strip().lower()
    if explicit_mode == "hard":
        enabled = False
    elif explicit_mode == "native":
        enabled = True
    if os.getenv("CC_HARNESS_DISABLE_NATIVE_FALLBACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        enabled = False
    # Benchmark/evaluation runs must never silently switch to the host, even
    # when an inherited shell happens to contain the native opt-in variable.
    if benchmark_process:
        enabled = False
    _session_allow_native_fallback = bool(enabled) and not restricted_profile
    _session_fallback_profile = profile


def session_executor_status() -> dict[str, object]:
    """Return redaction-safe backend/fallback status for UI and audit paths."""

    config = _session_executor_config
    backend = getattr(config, "backend", None)
    return {
        "initialized": _session_executor is not None,
        "backend": backend.value if isinstance(backend, ExecutorBackend) else (
            str(backend) if backend is not None else None
        ),
        "native_fallback_enabled": bool(_session_allow_native_fallback),
        "native_fallback_active": isinstance(_session_executor, NativeExecutor)
        and bool(_session_fallback_reason),
        "fallback_profile": _session_fallback_profile,
        "fallback_reason": _session_fallback_reason,
    }


def get_session_executor() -> Executor:
    """Return the explicitly initialized executor or fail closed."""
    if _session_executor is None:
        raise ExecutorNotInitializedError(
            "session executor is not initialized; command was not executed"
        )
    return _session_executor


async def prewarm_session_executor():
    """Eagerly start the selected executor's service dependencies.

    Native execution has no service to prewarm.  SandboxExecutor exposes a
    small ``prewarm_server`` capability; keeping this adapter here avoids
    importing optional OpenSandbox modules during normal package import and
    gives Durable Runtime one lifecycle seam for startup readiness.
    """
    executor = get_session_executor()
    prewarm = getattr(executor, "prewarm_server", None)
    if prewarm is None:
        return None
    return await prewarm()


def reset_session_executor() -> None:
    """Clear session executor state for test and lifecycle isolation."""
    global _session_executor, _session_executor_config
    global _session_allow_native_fallback, _session_fallback_profile
    global _session_fallback_reason
    _session_executor = None
    _session_executor_config = None
    _session_allow_native_fallback = False
    _session_fallback_profile = None
    _session_fallback_reason = None


async def shutdown_session_executor() -> None:
    """repl 退出调:sandbox 时 kill 容器 + shutdown_owned_server;native 无副作用。

    全部 best-effort:任何异常吞掉(退出路径不能炸)。NativeExecutor 无 kill
    方法 → getattr 返回 None → 跳过。
    """
    global _session_executor, _session_executor_config
    global _session_allow_native_fallback, _session_fallback_profile
    global _session_fallback_reason
    if _session_executor is None:
        _session_executor_config = None
        _session_allow_native_fallback = False
        _session_fallback_profile = None
        _session_fallback_reason = None
        return
    kill = getattr(_session_executor, "kill", None)
    if kill is not None:
        try:
            await kill()
        except Exception:
            pass
    try:
        from cc_harness.sandbox_server import shutdown_owned
        await shutdown_owned()
    except Exception:
        pass
    _session_executor = None
    _session_executor_config = None
    _session_allow_native_fallback = False
    _session_fallback_profile = None
    _session_fallback_reason = None


def _native_fallback_audit(project_root: Path, *, reason: str, stage: str, retry_current: bool) -> None:
    """Persist a redaction-safe record whenever the host fallback is armed."""

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": "native_fallback_activated",
        "reason": reason,
        "stage": stage,
        "retry_current": bool(retry_current),
        "profile": _session_fallback_profile,
    }
    path = project_root / ".cc-harness" / "logs" / "sandbox.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # The fallback must not mask the original infrastructure diagnosis.
        pass


async def fallback_session_executor(
    error: BaseException,
    *,
    retry_current: bool,
) -> bool:
    """Arm NativeExecutor after a sandbox infrastructure outage.

    Returns ``True`` when native execution is active.  This function never
    executes a command itself; callers may replay only when ``retry_current``
    is true (the sandbox raised before dispatch).  A command-transport error
    can therefore switch the *next* action to native without duplicating an
    action whose outcome is unknown.
    """

    global _session_executor, _session_executor_config, _session_fallback_reason
    if not isinstance(error, BaseException):
        return False
    if not bool(getattr(error, "fallback_safe", True)):
        return False
    async with _session_fallback_lock:
        config = _session_executor_config
        if config is None or config.backend is not ExecutorBackend.SANDBOX:
            return isinstance(_session_executor, NativeExecutor) and bool(_session_fallback_reason)
        if not _session_allow_native_fallback:
            return False
        current = _session_executor
        project_root = getattr(current, "project_root", None)
        if project_root is None:
            return False
        if isinstance(current, NativeExecutor):
            return True
        kill = getattr(current, "kill", None)
        if kill is not None:
            try:
                await kill()
            except Exception:
                # Teardown is best effort; the old backend is discarded and
                # no command is replayed until the new executor is ready.
                pass
        try:
            from cc_harness.sandbox_server import shutdown_owned
            await shutdown_owned()
        except Exception:
            pass
        config.backend = ExecutorBackend.NATIVE
        native = build_executor(config, Path(project_root))
        _session_executor = native
        _session_fallback_reason = str(error)
        _native_fallback_audit(
            Path(project_root),
            reason=str(error),
            stage=str(getattr(error, "stage", "preflight")),
            retry_current=retry_current,
        )
        return True


async def run_command(
    args: dict | str,
    *,
    cwd: str = ".",
    use_pty: bool = False,
    pty_writer: Callable[[bytes], Awaitable[None]] | None = None,
    timeout_s: float | None = None,
) -> ToolResult | int:
    """Built-in shell tool; optionally stream combined output through a POSIX PTY.

    The dictionary API is the existing native-tool interface and remains unchanged
    when ``use_pty`` is false.  The scalar command form is accepted for the web UI
    PTY interface and returns the subprocess exit code.
    """
    if use_pty:
        try:
            executor = get_session_executor()
        except ExecutorNotInitializedError as exc:
            if pty_writer is not None:
                await pty_writer(f"[Tool Error] {exc}\n".encode())
            return 126
        if not isinstance(executor, NativeExecutor):
            message = "PTY host execution requires the explicit native backend"
            if pty_writer is not None:
                await pty_writer(f"[Tool Error] {message}\n".encode())
            return 126
        if os.name != "posix":
            raise NotImplementedError("PTY only supported on POSIX")
        import pty
        import select

        command = args if isinstance(args, str) else args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return 1
        master_fd, slave_fd = pty.openpty()
        proc = None
        try:
            process_options: dict[str, object] = {"start_new_session": True}
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=str(executor.project_root),
                env=executor._build_env(),
                **process_options,
            )
            os.close(slave_fd)
            slave_fd = -1
            requested_timeout = timeout_s if timeout_s is not None else RUN_COMMAND_TIMEOUT_S
            task_budget = remaining_task_budget()
            if task_budget is not None:
                requested_timeout = min(float(requested_timeout), task_budget)
            deadline = asyncio.get_running_loop().time() + max(0.1, float(requested_timeout))
            loop = asyncio.get_running_loop()
            while True:
                if loop.time() >= deadline:
                    await _terminate_process_tree(proc)
                    return 124
                readable, _, _ = await loop.run_in_executor(
                    None, lambda: select.select([master_fd], [], [], 0.05)
                )
                if readable:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if chunk and pty_writer is not None:
                        await pty_writer(chunk)
                if proc.returncode is not None and not readable:
                    break
            await proc.wait()
            return proc.returncode if proc.returncode is not None else 1
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    await _terminate_process_tree(proc)
                except ProcessLookupError:
                    pass
            if slave_fd >= 0:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            try:
                os.close(master_fd)
            except OSError:
                pass

    # Existing non-PTY executor path. Keep this branch's behavior intact.
    scalar_command = isinstance(args, str)
    if scalar_command:
        args = {"command": args}
    else:
        # Do not mutate the model-owned argument object while adding local
        # retry bookkeeping.  Executor implementations intentionally ignore
        # these control fields, but a fresh mapping keeps that contract clear.
        args = dict(args)
    command_text = args.get("command", "")
    retry_requested = bool(args.get("retry_on_network", False))
    retry_safe = is_network_operation(command_text if isinstance(command_text, str) else "")
    retry_limit = resolve_network_retry_limit(
        args.get("network_retry_limit"), enabled=retry_requested and retry_safe
    )
    retry_backoff = resolve_network_retry_backoff(args.get("network_retry_backoff_s"))
    retry_attempts = 0
    retry_events: list[dict[str, object]] = []
    from cc_harness.sandbox import SandboxUnavailableError
    while True:
        retry_attempts += 1
        try:
            result = await get_session_executor().run(args, cwd=Path(cwd))
        except ExecutorNotInitializedError as exc:
            result = ToolResult.error(
                display="executor not initialized; command was not executed",
                llm=f"[Tool Error] {exc}",
            )
        except SandboxUnavailableError as exc:
            # Only a failure before dispatch may replay the current command.
            # If the SDK transport failed after dispatch, arm native execution
            # for the next action but preserve this action as outcome-unknown.
            safe_to_retry = bool(getattr(exc, "retry_safe", False))
            switched = await fallback_session_executor(
                exc,
                retry_current=safe_to_retry,
            )
            if switched and safe_to_retry:
                result = await get_session_executor().run(args, cwd=Path(cwd))
            elif switched:
                result = ToolResult.error(
                    display="sandbox unavailable; current command outcome is unknown",
                    llm=(
                        "[Tool Error] sandbox transport failed after dispatch; the command was "
                        "not replayed. Native execution is enabled for subsequent commands. "
                        f"stage={getattr(exc, 'stage', 'command_transport')}: {exc}"
                    ),
                    metadata={
                        "exit_code": None,
                        "outcome_unknown": True,
                        "sandbox_fallback": "native_for_next_action",
                        "sandbox_stage": getattr(exc, "stage", "command_transport"),
                    },
                )
            else:
                result = ToolResult.error(
                    display="sandbox unavailable; command was not executed",
                    llm=(
                        "[Tool Error] sandbox unavailable; command was not executed and "
                        f"native fallback is disabled: {exc}"
                    ),
                    metadata={
                        "exit_code": None,
                        "outcome_unknown": False,
                        "sandbox_stage": getattr(exc, "stage", "preflight"),
                    },
                )
        result_text = "\n".join(
            str(value or "")
            for value in (
                getattr(result, "display_text", ""),
                getattr(result, "llm_text", ""),
                (getattr(result, "metadata", {}) or {}).get("stdout", "")
                if isinstance(getattr(result, "metadata", {}), dict)
                else "",
                (getattr(result, "metadata", {}) or {}).get("stderr", "")
                if isinstance(getattr(result, "metadata", {}), dict)
                else "",
            )
        )
        should_retry = (
            retry_limit > 0
            and bool(getattr(result, "is_error", False))
            and is_transient_network_failure(result_text)
            and retry_attempts <= retry_limit
        )
        if not should_retry:
            break
        delay = min(30.0, retry_backoff * (2 ** (retry_attempts - 1)))
        remaining = remaining_task_budget()
        if remaining is not None and remaining <= delay:
            break
        retry_events.append(
            {
                "attempt": retry_attempts,
                "delay_s": round(delay, 3),
                "reason": "transient_network_failure",
            }
        )
        await asyncio.sleep(delay)

    if retry_requested:
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["network_retry"] = {
            "requested": True,
            "safe_command": retry_safe,
            "limit": retry_limit,
            "attempts": retry_attempts,
            "retries": len(retry_events),
            "events": retry_events,
            "exhausted": bool(retry_limit and len(retry_events) >= retry_limit),
            "refused": "command_not_idempotent" if not retry_safe else None,
        }
        # ToolResult is mutable for backwards compatibility with MCP results.
        # A custom executor may return a duck-typed result, so only assign when
        # the attribute is writable.
        try:
            result.metadata = metadata
        except (AttributeError, TypeError):
            pass
        if bool(getattr(result, "is_error", False)):
            try:
                result.llm_text = (
                    f"{result.llm_text}\n[Network retry] attempted "
                    f"{retry_attempts} time(s); safe={str(retry_safe).lower()}"
                )
            except (AttributeError, TypeError):
                pass
    if scalar_command:
        return 0 if not result.is_error else 1
    return result


async def process_status(args: dict, *, cwd: str = ".") -> ToolResult:
    """Read a persisted background-process handle without touching the process."""

    del cwd
    try:
        pid = int(args.get("pid"))
    except (TypeError, ValueError):
        return ToolResult.error(
            "'pid' must be a positive integer",
            "[Tool Error] 'pid' must be a positive integer",
        )
    if pid < 1:
        return ToolResult.error(
            "'pid' must be a positive integer",
            "[Tool Error] 'pid' must be a positive integer",
        )
    try:
        executor = get_session_executor()
    except ExecutorNotInitializedError as exc:
        return ToolResult.error(
            "executor not initialized; background status was not read",
            f"[Tool Error] {exc}",
            metadata={"pid": pid, "background": True, "state": "unknown"},
        )
    status = getattr(executor, "background_status", lambda _pid: None)(pid)
    if status is None:
        return ToolResult.error(
            f"background process {pid} is not owned by this project",
            f"[Tool Error] background process {pid} is not owned by this project",
            metadata={"pid": pid, "background": True, "state": "unknown"},
        )
    encoded = json.dumps(status, ensure_ascii=False, sort_keys=True)
    return ToolResult.success(
        encoded,
        metadata={"pid": pid, "background": True, **status},
    )


async def service_status(args: dict, *, cwd: str = ".") -> ToolResult:
    """Inspect both liveness and an optional service health probe."""

    del cwd
    try:
        pid = int(args.get("pid"))
    except (TypeError, ValueError):
        return ToolResult.error(
            "'pid' must be a positive integer",
            "[Tool Error] 'pid' must be a positive integer",
        )
    if pid < 1:
        return ToolResult.error(
            "'pid' must be a positive integer",
            "[Tool Error] 'pid' must be a positive integer",
        )
    try:
        executor = get_session_executor()
    except ExecutorNotInitializedError as exc:
        return ToolResult.error(
            "executor not initialized; service status was not read",
            f"[Tool Error] {exc}",
            metadata={"pid": pid, "service": True, "state": "unknown"},
        )
    checker = getattr(executor, "managed_service_status", None)
    if checker is None:
        return ToolResult.error(
            "managed service status is unavailable for the selected executor",
            "[Tool Error] managed service status is unavailable for the selected executor",
            metadata={"pid": pid, "service": True, "state": "unsupported"},
        )
    status = await checker(pid, probe=bool(args.get("probe", True)))
    if status is None:
        return ToolResult.error(
            f"managed service {pid} is not owned by this project",
            f"[Tool Error] managed service {pid} is not owned by this project",
            metadata={"pid": pid, "service": True, "state": "unknown"},
        )
    encoded = json.dumps(status, ensure_ascii=False, sort_keys=True)
    healthy = (status.get("health") or {}).get("status")
    return ToolResult.success(
        encoded,
        metadata={"pid": pid, "service": True, "health_status": healthy, **status},
    )


async def process_stop(args: dict, *, cwd: str = ".") -> ToolResult:
    """Stop one persisted background process after the normal policy gate."""

    del cwd
    try:
        pid = int(args.get("pid"))
    except (TypeError, ValueError):
        return ToolResult.error(
            "'pid' must be a positive integer",
            "[Tool Error] 'pid' must be a positive integer",
        )
    if pid < 1:
        return ToolResult.error(
            "'pid' must be a positive integer",
            "[Tool Error] 'pid' must be a positive integer",
        )
    try:
        executor = get_session_executor()
    except ExecutorNotInitializedError as exc:
        return ToolResult.error(
            "executor not initialized; background process was not stopped",
            f"[Tool Error] {exc}",
            metadata={"pid": pid, "background": True, "state": "unknown"},
        )
    stopper = getattr(executor, "stop_background", None)
    if stopper is None:
        return ToolResult.error(
            "background process control is unavailable",
            "[Tool Error] background process control is unavailable",
            metadata={"pid": pid, "background": True, "state": "unsupported"},
        )
    stopped = bool(await stopper(pid))
    if not stopped:
        return ToolResult.error(
            f"background process {pid} could not be stopped safely",
            f"[Tool Error] background process {pid} could not be stopped safely",
            metadata={"pid": pid, "background": True, "state": "unknown"},
        )
    return ToolResult.success(
        f"background process {pid} stopped",
        metadata={"pid": pid, "background": True, "state": "stopped"},
    )


# OpenAI function-calling spec for run_command — matches the shape produced
# by mcp_client.list_tools() so the LLM client sees a unified tool list.
RUN_COMMAND_SPEC = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Execute a shell command through the configured session executor and return stdout. "
            "The command runs in the project root with a bounded timeout. "
            "Dangerous commands (rm -rf, format, drop database, etc.) require "
            "user confirmation. Use this for running scripts, git commands, "
            "listing files, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The shell command to execute. The active session replaces this text "
                        "with its exact platform and command dialect before model use."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Start an explicitly long-lived background process. The result includes "
                        "a PID and stdout/stderr log paths; it is not subject to the foreground "
                        "idle timeout. Use only for a service or watcher that must outlive this call."
                    ),
                },
                "readiness_command": {
                    "type": "string",
                    "description": (
                        "Optional bounded health/readiness command polled after a background "
                        "process starts. Exit code 0 marks readiness=ready; a timeout leaves "
                        "the process running with readiness=timeout for later status checks."
                    ),
                },
                "readiness_timeout_s": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 120,
                    "default": 15,
                    "description": "Maximum seconds to wait for the optional readiness command.",
                },
                "service_name": {
                    "type": "string",
                    "description": "Stable service name recorded in the lifecycle manifest.",
                },
                "health_command": {
                    "type": "string",
                    "description": (
                        "Optional bounded health command. It is queried via service_status; "
                        "process liveness and health are reported separately."
                    ),
                },
                "health_timeout_s": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 30,
                    "default": 2,
                    "description": "Maximum seconds for a service health probe.",
                },
                "shutdown_timeout_s": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 30,
                    "default": 5,
                    "description": "Grace period used when stopping a managed service.",
                },
                "retry_on_network": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Ask the runtime to retry a transient network/dependency failure. "
                        "Retries are only honored for recognized idempotent package/download "
                        "commands (apt, pip, uv, npm, curl, wget, git fetch/clone, docker pull); "
                        "arbitrary shell commands and mutating operations are never retried."
                    ),
                },
                "network_retry_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                    "default": 0,
                    "description": (
                        "Maximum additional attempts for a requested network retry. The model "
                        "chooses the budget, but the runtime hard-caps it at 10."
                    ),
                },
                "network_retry_backoff_s": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 30,
                    "default": 1,
                    "description": "Base seconds for bounded exponential backoff between network retries.",
                },
            },
            "required": ["command"],
        },
    },
}


BACKGROUND_STATUS_SPEC = {
    "type": "function",
    "function": {
        "name": "process_status",
        "description": (
            "Inspect a previously started background process by PID. Returns state, exit code, "
            "readiness, activity counters, and stdout/stderr log paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "minimum": 1}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
}


SERVICE_STATUS_SPEC = {
    "type": "function",
    "function": {
        "name": "service_status",
        "description": (
            "Inspect a managed service's process liveness and optional health probe. "
            "An alive process is not automatically healthy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "minimum": 1},
                "probe": {"type": "boolean", "default": True},
            },
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
}


BACKGROUND_STOP_SPEC = {
    "type": "function",
    "function": {
        "name": "process_stop",
        "description": (
            "Safely stop a background process previously started in this project. The action "
            "is an external side effect and may require approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "minimum": 1}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
}


def _surface_shell_metadata(executor: Executor) -> None:
    profile = getattr(executor, "shell_profile", None)
    if profile is None:
        description = (
            "The command to execute in the configured sandbox's POSIX shell. "
            "Use POSIX shell syntax."
        )
    else:
        examples = (
            " Prefer Get-ChildItem and Get-Content for listing and reading files."
            if profile.dialect == "powershell"
            else ""
        )
        description = (
            f"The command to execute with {profile.name} on {profile.platform}. "
            f"Use the {profile.dialect} command dialect.{examples}"
        )
    RUN_COMMAND_SPEC["function"]["parameters"]["properties"]["command"][
        "description"
    ] = description
