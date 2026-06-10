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
    """Yellow: emit a tool invocation summary."""
    args_str = json.dumps(arguments, ensure_ascii=False)
    console.print(f"\n→ {name} {args_str}", style="yellow")
    _flush(console)

def print_tool_result(console: Console, text: str, is_error: bool = False) -> None:
    """Green on success, red on error."""
    style = "red" if is_error else "green"
    label = "✗ tool result" if is_error else "✓ tool result"
    console.print(Panel(text, title=label, border_style=style, expand=False))
    _flush(console)

def print_final(console: Console, text: str) -> None:
    """White: the LLM's final answer, rendered as Markdown so code blocks highlight."""
    console.print()
    console.print(Markdown(text), style="white")
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
