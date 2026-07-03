"""Dangerous-command detection + user confirmation prompt + built-in tools."""
from __future__ import annotations
import re
import sys
from pathlib import Path
from typing import Optional

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


def confirm_tool(tool_name: str, args: dict) -> str:
    """3-way confirmation for the L4 policy gate. Returns 'yes' / 'always' / 'no'.

    Default is 'no' (Enter = no). EOF / Ctrl-C → 'no' (fail-closed).
    """
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
_session_executor: Optional[Executor] = None


def init_session_executor(config: ExecutorConfig, project_root: str | Path) -> None:
    """repl 启动调:按 config.backend 建会话级 executor(native 或 sandbox)。

    project_root 锁执行 cwd;sandbox 在容器内 mount 该根为只读。
    """
    global _session_executor
    _session_executor = build_executor(config, Path(project_root))


def get_session_executor() -> Executor:
    """run_command 取;未 init(repl 外,如测试/脚本)lazy 兜底 NativeExecutor。

    lazy 路径读 RUN_COMMAND_TIMEOUT_S AT CALL TIME(与历史行为一致,允许测试
    monkeypatch 该常量)。project_root 默认当前目录(repl 外调用语义)。
    """
    global _session_executor
    if _session_executor is None:
        _session_executor = NativeExecutor(
            project_root=Path("."), timeout_s=RUN_COMMAND_TIMEOUT_S,
        )
    return _session_executor


def reset_session_executor() -> None:
    """测试隔离:清空单例,使下次 get 重新 lazy-init。"""
    global _session_executor
    _session_executor = None


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


def _native_fallback(cwd: str) -> NativeExecutor:
    """sandbox 降级用的 native executor(隔离 cwd = per-call cwd)。"""
    return NativeExecutor(project_root=Path(cwd), timeout_s=RUN_COMMAND_TIMEOUT_S)


async def run_command(args: dict, *, cwd: str = ".") -> ToolResult:
    """Built-in shell tool。走会话级 executor;sandbox 连败 3 次
    (_with_retry 内)抛 SandboxUnavailableError → 降级 native + 警告。

    注意:会话路径中 cwd 被忽略——工作目录由 init_session_executor 的
    project_root 决定(沙箱 mount / NativeExecutor 锁项目根)。cwd 仅在
    降级路径(_native_fallback)构造 NativeExecutor 时使用。

    执行加固(cwd 锁项目根 / env-strip 密钥 / 超时)在 NativeExecutor;
    sandbox 时这些由容器隔离承担。权限决策(allow/ask)由 agent 层
    PolicyEngine 在派发前完成,本函数不再 gate。timeout_s 仍 AT CALL TIME
    读 RUN_COMMAND_TIMEOUT_S(测试可 monkeypatch)。
    """
    from cc_harness.sandbox import SandboxUnavailableError
    try:
        return await get_session_executor().run(args, cwd=Path(cwd))
    except SandboxUnavailableError:
        print("[warn] 沙箱不可用,降级 native 执行(非隔离模式)", file=sys.stderr)
        return await _native_fallback(cwd).run(args, cwd=Path(cwd))


# OpenAI function-calling spec for run_command — matches the shape produced
# by mcp_client.list_tools() so the LLM client sees a unified tool list.
RUN_COMMAND_SPEC = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Execute a shell command on the local machine and return its stdout. "
            "The command runs in the project root with a 30s timeout. "
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
                        "The shell command to execute. Passed to the system "
                        "shell (sh -c on Unix, cmd /c on Windows)."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}
