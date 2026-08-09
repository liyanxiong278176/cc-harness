"""Rich projections for :mod:`cc_harness.terminal.transcript`."""
from __future__ import annotations

import json
import re
from io import StringIO

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from cc_harness.terminal.transcript import (
    AssistantBlock,
    ToolActivity,
    TranscriptState,
    TurnTranscript,
)


_CONTROL_SEQUENCE_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPINNERS = ("✻", "✽", "✶", "✳")


def sanitize_terminal_text(value: str) -> str:
    """Remove cursor/title/link control sequences supplied by model or tools."""
    return _UNSAFE_CONTROL_RE.sub("", _CONTROL_SEQUENCE_RE.sub("", value))


class TranscriptRichRenderer:
    def __init__(self, *, accent: str = "color(209)") -> None:
        self.accent = accent

    def render(
        self,
        state: TranscriptState,
        *,
        width: int,
        color: bool,
        detailed: bool = False,
        focus: bool = False,
        now: float | None = None,
    ) -> str:
        output = StringIO()
        console = Console(
            file=output,
            force_terminal=color,
            color_system="truecolor" if color else None,
            width=max(20, width),
            highlight=False,
            legacy_windows=False,
        )
        turns = state.turns[-1:] if focus and state.turns else state.turns
        for index, turn in enumerate(turns):
            if index:
                console.print()
            self._render_turn(console, state, turn, width=width, detailed=detailed, now=now)
        for style, notice in state.pending_notices:
            icon, rich_style = {
                "error": ("Error:", "red"),
                "warning": ("▲", "yellow"),
                "info": ("●", "cyan"),
            }.get(style, ("●", "dim"))
            console.print(Text.assemble((icon + " ", rich_style), sanitize_terminal_text(notice)))
        return output.getvalue()

    def _render_turn(
        self,
        console: Console,
        state: TranscriptState,
        turn: TurnTranscript,
        *,
        width: int,
        detailed: bool,
        now: float | None,
    ) -> None:
        self._render_user(console, turn, width)
        if turn.thought_seconds is not None:
            console.print(Text.assemble(
                ("Thought for ", "dim"), (self._duration(turn.thought_seconds), "bold dim")
            ))
            console.print()
        elif turn.status == "active" and not turn.items and not turn.stream_text:
            thinking = state.thinking_seconds(turn, now=now)
            if thinking >= 0.3:
                frame = _SPINNERS[int(thinking * 5) % len(_SPINNERS)]
                console.print(Text.assemble((frame, f"bold {self.accent}"), (" Thinking…", "dim")))

        for item in turn.items:
            if isinstance(item, AssistantBlock):
                self._render_assistant(console, item.text)
            else:
                self._render_tool(console, item, detailed=detailed)
        if turn.stream_text:
            self._render_assistant(console, turn.stream_text)

        if turn.retry_at is not None:
            seconds = state.retry_seconds(turn, now=now)
            attempt = ""
            if turn.retry_attempt is not None:
                attempt = f" (attempt {turn.retry_attempt}"
                if turn.retry_max is not None:
                    attempt += f"/{turn.retry_max}"
                attempt += ")"
            reason = f" · {turn.retry_reason}" if turn.retry_reason else ""
            console.print(Text(
                f"Retrying in {seconds or 0}s…{attempt}{reason}", style="yellow"
            ))

        if turn.status == "success":
            duration = self._duration(turn.duration_seconds or 0)
            console.print(Text.assemble(
                ("✻ ", f"bold {self.accent}"),
                (f"{turn.completion_verb} for {duration}", "dim"),
            ))
        elif turn.status == "interrupted":
            console.print(Text("Interrupted", style="dim"))
        elif turn.status == "failed":
            console.print(Text.assemble(
                ("Failed", "bold red"),
                ((f" · {sanitize_terminal_text(turn.error)}") if turn.error else "", "red"),
            ))

    @staticmethod
    def _render_user(console: Console, turn: TurnTranscript, width: int) -> None:
        text = sanitize_terminal_text(turn.prompt)
        lines = text.splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "❯ " if index == 0 else "  "
            value = prefix + line
            console.print(Text(value.ljust(max(1, width)), style="on #383838"), overflow="crop")
        for attachment in turn.attachments:
            value = f"  ▣ {sanitize_terminal_text(attachment)}"
            console.print(Text(value.ljust(max(1, width)), style="magenta on #383838"), overflow="crop")
        console.print()

    @staticmethod
    def _render_assistant(console: Console, text: str) -> None:
        clean = sanitize_terminal_text(text)
        grid = Table.grid(expand=True, padding=0)
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(Text("● ", style="bold white"), Markdown(clean))
        console.print(grid)
        console.print()

    @staticmethod
    def _render_tool(console: Console, tool: ToolActivity, *, detailed: bool) -> None:
        args = tool.args if detailed else _key_args(tool.args)
        suffix = json.dumps(args, ensure_ascii=False, separators=(",", ":")) if args else ""
        color = "red" if tool.is_error else ("yellow" if tool.status == "running" else "white")
        console.print(Text.assemble(
            ("● ", f"bold {color}"),
            (tool.name, "bold"),
            ((f"({suffix})") if suffix else "", "dim"),
        ))
        result = tool.output if detailed and tool.output else tool.summary
        result = sanitize_terminal_text(result)
        style = "red" if tool.is_error else "dim"
        console.print(Padding(Text("⎿ " + result, style=style), (0, 0, 0, 2)))
        console.print()

    @staticmethod
    def _duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"


def _key_args(args: dict) -> dict:
    for key in ("path", "file_path", "filePath", "command", "query", "url", "pattern"):
        if key in args:
            value = args[key]
            if isinstance(value, str) and len(value) > 120:
                value = value[:119] + "…"
            return {key: value}
    if not args:
        return {}
    key = next(iter(args))
    return {key: args[key]}
