"""Dangerous-command detection + user confirmation prompt + built-in tools."""
from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from cc_harness.config import ExecutorConfig
from cc_harness.executor import Executor, NativeExecutor, build_executor
from cc_harness.mcp_client import ToolResult

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


class ExecutorNotInitializedError(RuntimeError):
    """Raised when no session executor was explicitly selected."""


def init_session_executor(config: ExecutorConfig, project_root: str | Path) -> None:
    """repl 启动调:按 config.backend 建会话级 executor(native 或 sandbox)。

    project_root 锁执行 cwd;sandbox 在容器内 mount 该根为只读。
    Store config for status and diagnostics.
    """
    global _session_executor, _session_executor_config
    _session_executor = build_executor(config, Path(project_root))
    _session_executor_config = config
    _surface_shell_metadata(_session_executor)


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
    _session_executor = None
    _session_executor_config = None


async def shutdown_session_executor() -> None:
    """repl 退出调:sandbox 时 kill 容器 + shutdown_owned_server;native 无副作用。

    全部 best-effort:任何异常吞掉(退出路径不能炸)。NativeExecutor 无 kill
    方法 → getattr 返回 None → 跳过。
    """
    global _session_executor
    if _session_executor is None:
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
        import asyncio
        import pty
        import select

        command = args if isinstance(args, str) else args.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return 1
        master_fd, slave_fd = pty.openpty()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=str(executor.project_root),
                env=executor._build_env(),
            )
            os.close(slave_fd)
            slave_fd = -1
            deadline = asyncio.get_running_loop().time() + (
                timeout_s if timeout_s is not None else RUN_COMMAND_TIMEOUT_S
            )
            loop = asyncio.get_running_loop()
            while True:
                if loop.time() >= deadline:
                    proc.kill()
                    await proc.wait()
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
                    proc.kill()
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
    from cc_harness.sandbox import SandboxUnavailableError
    try:
        result = await get_session_executor().run(args, cwd=Path(cwd))
    except ExecutorNotInitializedError as exc:
        result = ToolResult.error(
            display="executor not initialized; command was not executed",
            llm=f"[Tool Error] {exc}",
        )
    except SandboxUnavailableError as exc:
        result = ToolResult.error(
            display="sandbox unavailable; command was not executed",
            llm=("[Tool Error] sandbox unavailable; command was not executed and host fallback "
                 f"is disabled: {exc}"),
        )
    if scalar_command:
        return 0 if not result.is_error else 1
    return result


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
            },
            "required": ["command"],
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
