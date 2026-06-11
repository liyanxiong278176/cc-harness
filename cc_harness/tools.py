"""Dangerous-command detection + user confirmation prompt + built-in tools."""
from __future__ import annotations
import asyncio
import re
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
    """Return True if this tool call is dangerous and needs user confirmation.

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


# --- Built-in tools (registered as native functions, not via MCP) ---

# Per-call timeout for run_command. Long enough for most builds/tests,
# short enough to surface hangs fast.
RUN_COMMAND_TIMEOUT_S = 30


async def run_command(args: dict, *, cwd: str = ".") -> ToolResult:
    """Built-in tool: execute a shell command and return its stdout.

    Uses asyncio subprocess (non-blocking) so the event loop stays responsive.
    Subject to the same is_dangerous + confirm gate as MCP shell tools —
    dangerous patterns (rm -rf, format, drop database, etc.) require explicit
    user confirmation before execution.

    Returns ToolResult with stdout on success, or a descriptive error message
    on non-zero exit / timeout / exception.
    """
    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return ToolResult.error(
            display="'command' must be a non-empty string",
            llm="[Tool Error] 'command' must be a non-empty string",
        )

    if is_dangerous("run_command", args):
        if not confirm("Confirm execution?"):
            return ToolResult.error(
                display="user rejected dangerous command",
                llm=f"[Tool Error] user rejected dangerous command: {command}",
            )

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=RUN_COMMAND_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return ToolResult.error(
                display=f"timeout after {RUN_COMMAND_TIMEOUT_S}s",
                llm=f"[Tool Error] timeout after {RUN_COMMAND_TIMEOUT_S}s",
            )
    except Exception as e:
        return ToolResult.error(
            display=f"raised: {e}",
            llm=f"[Tool Error] {type(e).__name__}: {e}",
        )

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

    if proc.returncode != 0:
        combined = (stdout + stderr).strip() or f"(no output, exit {proc.returncode})"
        return ToolResult.error(
            display=f"exit {proc.returncode}: {combined[:200]}",
            llm=f"[Tool Error] exit {proc.returncode}\nstdout: {stdout}\nstderr: {stderr}",
        )
    return ToolResult.success(stdout if stdout else "(no output)")


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
