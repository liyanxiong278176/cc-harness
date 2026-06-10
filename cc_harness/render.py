# cc_harness/render.py
"""Rich color wrappers. All public functions take a Console instance."""
from __future__ import annotations
import json
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

def print_thought(console: Console, text: str) -> None:
    """Blue: stream LLM 'thinking' tokens as they arrive."""
    console.print(text, style="blue", end="", highlight=False)

def print_tool_call(console: Console, name: str, arguments: dict) -> None:
    """Yellow: emit a tool invocation summary."""
    args_str = json.dumps(arguments, ensure_ascii=False)
    console.print(f"\n→ {name} {args_str}", style="yellow")

def print_tool_result(console: Console, text: str, is_error: bool = False) -> None:
    """Green on success, red on error."""
    style = "red" if is_error else "green"
    label = "✗ tool result" if is_error else "✓ tool result"
    console.print(Panel(text, title=label, border_style=style, expand=False))

def print_final(console: Console, text: str) -> None:
    """White: the LLM's final answer, rendered as Markdown so code blocks highlight."""
    console.print()
    console.print(Markdown(text), style="white")

def print_warn(console: Console, text: str) -> None:
    console.print(f"[yellow]⚠ {text}[/yellow]")

def print_error(console: Console, text: str) -> None:
    console.print(f"[red]✗ {text}[/red]")

def print_info(console: Console, text: str) -> None:
    console.print(f"[cyan]{text}[/cyan]")
