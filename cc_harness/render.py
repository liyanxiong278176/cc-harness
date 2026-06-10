"""Plain-text renderers for the cc-harness REPL — 4-phase ReAct format.

Output structure (per the user spec):
  思考: <LLM's full reasoning text for this iteration>
  行动: <tool_name>
    arg1: val1
    arg2: val2
  观察: <tool's actual result text — what the LLM sees>
  [loop, until no more tool calls]
  结果: <LLM's final answer>

Design rules:
  - No colors. Only unicode glyphs (⚠ ✗) for visual structure.
  - 思考 is the COMPLETE text the LLM emitted for that iteration (no
    truncation, no filtering — the user explicitly asked for "all text").
  - 观察 shows the raw tool result the LLM receives — this is the data
    the LLM reasons about. (Earlier design hid this; the new 4-phase
    ReAct format requires showing it.)
  - All four labels (思考 / 行动 / 观察 / 结果) are printed with the
    content on the same line, separated from each other by a blank line
    for visual clarity.
  - Each non-streaming phase is preceded by a blank line so blocks are
    visually separated (printed via _blank() which writes "\\n\\n" +
    flush; the leading double newline avoids the Rich print() bug where
    calling print() after end="" only emits 1 \\n).
  - All interactive print functions call console.file.flush() so streaming
    output is visible in real-time even when stdout is piped.
"""
from __future__ import annotations
import json
from rich.console import Console
from rich.markdown import Markdown


def _flush(console: Console) -> None:
    f = console.file
    if f is not None:
        try:
            f.flush()
        except (AttributeError, OSError):
            pass


def _blank(console: Console) -> None:
    """Force a blank line (one \\n) to the underlying file. We bypass Rich's
    print() because (a) Rich's print() with no args only emits 1 \\n when the
    cursor is mid-line, and (b) we want the blank line written to the
    underlying file (not via Rich's machinery) for full control."""
    f = console.file
    if f is not None:
        try:
            f.write("\n")
            f.flush()
        except (AttributeError, OSError):
            pass


def print_thought(console: Console, text: str) -> None:
    """Print '思考: <text>' — the LLM's full reasoning for this iteration.

    Per user spec: the COMPLETE text the LLM emitted, no truncation.
    A leading blank line separates it from the prior phase.
    """
    _blank(console)
    console.print(f"思考: {text}")
    _flush(console)


def print_action(console: Console, name: str, arguments: dict) -> None:
    """Print '行动: <name>' with one argument per line, blank line before."""
    _blank(console)
    console.print(f"行动: {name}")
    if arguments:
        for k, v in arguments.items():
            val_repr = json.dumps(v, ensure_ascii=False)
            # highlight=False stops Rich from colorizing the JSON-looking string
            console.print(f"  {k}: {val_repr}", highlight=False)
    _flush(console)


def print_observation(console: Console, text: str) -> None:
    """Print '观察: <text>' — the tool's actual result (what the LLM sees).

    The text should be the same string the LLM receives in the messages
    (i.e. ToolResult.llm_text, which includes "[Tool Error] ..." prefix for
    errors). For multi-line results (e.g. file contents), each line is
    indented under the label for readability.
    """
    _blank(console)
    console.print("观察:")
    for line in (text or "").splitlines() or [""]:
        console.print(f"  {line}")
    _flush(console)


def print_result(console: Console, text: str) -> None:
    """Print '结果: <text>' — the LLM's final answer, full text, with a
    blank line before."""
    _blank(console)
    console.print(f"结果: {text}")
    _flush(console)


def print_warn(console: Console, text: str) -> None:
    console.print(f"⚠ {text}")
    _flush(console)


def print_error(console: Console, text: str) -> None:
    console.print(f"✗ {text}")
    _flush(console)


def print_info(console: Console, text: str) -> None:
    console.print(text)
    _flush(console)
