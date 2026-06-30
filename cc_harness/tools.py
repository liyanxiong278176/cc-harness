"""Dangerous-command detection + user confirmation prompt + built-in tools."""
from __future__ import annotations
import re
from pathlib import Path
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


async def run_command(args: dict, *, cwd: str = ".") -> ToolResult:
    """Built-in shell tool. Execution hardening (cwd-lock / env-strip / timeout)
    lives in NativeExecutor.

    The permission decision (allow/ask) is made by the agent-layer PolicyEngine
    BEFORE dispatch; this function no longer gates. timeout_s is read at CALL
    TIME from this module's RUN_COMMAND_TIMEOUT_S (so tests can monkeypatch it).
    """
    from cc_harness.executor import NativeExecutor
    return await NativeExecutor(
        project_root=Path(cwd), timeout_s=RUN_COMMAND_TIMEOUT_S,
    ).run(args, cwd=Path(cwd))


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
