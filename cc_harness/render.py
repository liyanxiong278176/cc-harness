# cc_harness/render.py
"""Rich color wrappers. All public functions take a Console instance.

All interactive print functions call `console.file.flush()` after writing so
that streaming output is visible in real-time even when stdout is piped
(Python defaults to block-buffering for pipes, which would otherwise defer
all output until the buffer fills or the process exits).
"""
from __future__ import annotations
import json
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


def _flush(console: Console) -> None:
    """Force-flush the underlying stream so output appears immediately."""
    f = console.file
    if f is not None:
        try:
            f.flush()
        except (AttributeError, OSError):
            pass


def print_thought(console: Console, text: str) -> None:
    """Blue: stream LLM 'thinking' tokens as they arrive."""
    console.print(text, style="blue", end="", highlight=False)
    _flush(console)

def print_tool_call(console: Console, name: str, arguments: dict) -> None:
    """Yellow: emit a tool invocation with emoji + indented parameters."""
    console.print(f"🔧 调用工具: {name}", style="yellow")
    args_str = json.dumps(arguments, ensure_ascii=False, indent=2)
    for line in args_str.splitlines() or [""]:
        console.print(f"   {line}", style="dim")
    _flush(console)

def print_tool_result(console: Console, text: str, is_error: bool = False) -> None:
    """Green on success, red on error, with 📤 label and indented body."""
    style = "red" if is_error else "green"
    label = "✗ 执行结果" if is_error else "📤 执行结果:"
    console.print(label, style=style)
    for line in (text or "").splitlines() or [""]:
        console.print(f"   {line}", style=style)
    _flush(console)

def print_final(console: Console, text: str) -> None:
    """White: print a 'task done' marker followed by the final answer."""
    console.print()
    console.print("✅ 任务完成:", style="green")
    console.print(Markdown(text), style="white")
    _flush(console)

def print_done(console: Console) -> None:
    """Emit a ✅ done marker on its own line. Use when the LLM's streamed
    response already serves as the visible final text (so we just need a
    visual signal that the turn has ended, not a re-print of the text)."""
    console.print("✅", style="green")
    _flush(console)

def print_warn(console: Console, text: str) -> None:
    console.print(f"[yellow]⚠ {text}[/yellow]")
    _flush(console)

def print_error(console: Console, text: str) -> None:
    console.print(f"[red]✗ {text}[/red]")
    _flush(console)

def print_info(console: Console, text: str) -> None:
    console.print(f"[cyan]{text}[/cyan]")
    _flush(console)
