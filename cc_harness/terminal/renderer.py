"""Claude Code classic-style renderer whose transcript remains in scrollback."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cc_harness.release_notes import RELEASES, recent_items

_ACCENT = "color(209)"
_MASCOT_PIXELS = (
    "      BBB        BBB     ",
    "     BTTB       BTTTB    ",
    "    BTTTBBBBBBBBBTTTTB   ",
    "   BTTTTTTTTTTTTTTTTTTB  ",
    "  BTTTTTTTWWWWTTTTTTTTTB ",
    " BTTTTTUWWWWWWUUTTTTTTTTB",
    "BTTTTTTUWWWWWWUUTTTTTTTTB",
    "BTTTTTTUBBBBBWUUTTTTTTTTTB",
    "BTTTTTBBWWWWBTWUTTTTTTTTTB",
    " BTTTTBWWWWWWBTWTTTTTTTTB ",
    "  BTTBWWBBBBBWWBBTTTTTTB  ",
    "   BBBWWWWWWWWWBBTTTTBBB   ",
    "    BWWWWWWWWWWWBBBBB      ",
    "    BWWWWWWWWWWWB          ",
    "    BWWWWWWWWWWWB   BBB    ",
    "   BWWWWWWWWWWWWWBBTTTTTB  ",
    "  BWWWWWWWWWWWWWWWBTTTTB   ",
    " BBBBBBBBBBBBBBBBBBBBBBB    ",
)
_MASCOT_PIXELS_MINI = (
    "   BBB      BBB  ",
    "  BTTBBBBBBTTTB ",
    " BTTTTTTTTTTTTTB",
    "BTTTUWWUUTTTTTTB",
    "BTTTBBWUWTTTTTTB",
    " BBBWWWBTTTTBBB ",
    "  BWWWWWWWB BBB ",
    " BBBBBBBBBBBBBB ",
)
_MASCOT_COLORS = {
    "T": "#d8c3a0",  # tan head and tail
    "B": "#7a4333",  # brown outline and raised paw
    "U": "#2f65b7",  # blue eyes
    "W": "#f3f3ef",  # white face and body
}


def _context_label(tokens: int) -> str:
    if tokens >= 1_000_000:
        value = tokens / 1_000_000
        return f"{value:g}m"
    if tokens >= 1_000:
        value = tokens / 1_000
        return f"{value:g}k"
    return str(tokens)


class TerminalRenderer:
    def __init__(self, console: Console, *, verbose: bool = False, lang: str = "en") -> None:
        self.console = console
        self.verbose = verbose
        self.lang = lang
        self._streamed = ""
        self._events: list[dict] = []
        self._stream_started_at: float | None = None
        self._stream_chars = 0
        self.last_output_speed = 0.0

    def startup(self, runtime, version: str) -> None:
        terminal_width = max(40, self.console.size.width)
        panel_width = max(38, terminal_width - 2)
        body = self._startup_body(runtime, terminal_width)
        panel = Panel(
            body,
            title=Text.assemble((" cc-harness ", f"bold {_ACCENT}"), (f"v{version} ", "dim")),
            title_align="left",
            border_style=_ACCENT,
            box=box.ROUNDED,
            safe_box=False,
            padding=(0, 1),
            width=panel_width,
        )
        self.console.print(Padding(panel, (0, 0, 0, 1)))
        for _ in range(self._setting(runtime, "startup_blank_rows", 3)):
            self.console.print()

    def _startup_body(self, runtime, width: int):
        left = self._welcome_block(runtime)
        right = self._tips_block(runtime, compact=width < 120)
        if width < 80:
            return Group(left, Rule(style=_ACCENT), right)

        inner = max(32, width - 6)
        left_width = max(25, int(inner * 0.34))
        mascot_rows = (
            _MASCOT_PIXELS_MINI if self.console.size.height < 32 else _MASCOT_PIXELS
        )
        welcome_height = 5 + (len(mascot_rows) // 2)
        divider = Text("│\n" * (welcome_height - 1) + "│", style=_ACCENT)
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(width=left_width)
        grid.add_column(width=1)
        grid.add_column(ratio=1)
        grid.add_row(left, divider, right)
        return grid

    def _welcome_block(self, runtime):
        model = getattr(getattr(runtime, "llm", None), "model", "unconfigured")
        context_window = getattr(
            getattr(getattr(runtime, "state", None), "context_config", None),
            "context_window",
            0,
        )
        model_display = f"{model}[{_context_label(context_window)}]" if context_window else model
        mascot = self._mascot(compact=self.console.size.height < 32)
        meta = Text.assemble((model_display, "dim"), (" · API Usage Billing", "dim"))
        meta.no_wrap = True
        meta.overflow = "ellipsis"
        cwd = Text(str(runtime.cwd), style="dim", overflow="ellipsis", no_wrap=True)
        return Align.center(Group(
            Text("Welcome back!", style="bold", justify="center"),
            Text(""),
            mascot,
            Text(""),
            Align.center(meta),
            Align.center(cwd),
        ))

    @staticmethod
    def _mascot(*, compact: bool = False) -> Text:
        """Render the user-provided face-covering Yuexin Cat as terminal half-blocks."""
        pixels = _MASCOT_PIXELS_MINI if compact else _MASCOT_PIXELS
        width = max(len(row) for row in pixels)
        rows = [row.ljust(width) for row in pixels]
        result = Text(justify="center")
        for row_index in range(0, len(rows), 2):
            top = rows[row_index]
            bottom = rows[row_index + 1] if row_index + 1 < len(rows) else " " * width
            for top_pixel, bottom_pixel in zip(top, bottom, strict=True):
                top_color = _MASCOT_COLORS.get(top_pixel)
                bottom_color = _MASCOT_COLORS.get(bottom_pixel)
                if top_color is None and bottom_color is None:
                    result.append(" ")
                elif bottom_color is None:
                    result.append("▀", style=top_color)
                elif top_color is None:
                    result.append("▄", style=bottom_color)
                elif top_color == bottom_color:
                    result.append("█", style=top_color)
                else:
                    result.append("▀", style=f"{top_color} on {bottom_color}")
            if row_index + 2 < len(rows):
                result.append("\n")
        return result

    def _tips_block(self, runtime, *, compact: bool):
        cwd = str(runtime.cwd)
        note = f"Working directory: {cwd}"
        if compact and len(note) > 54:
            note = note[:51] + "..."
        item_limit = 48 if compact else 90
        items = [item if len(item) <= item_limit else item[:item_limit - 3] + "..."
                 for item in recent_items(3)]
        return Group(
            Text("Tips for getting started", style=f"bold {_ACCENT}", no_wrap=True),
            Text(
                "Run /init to create a CC-HARNESS.md file with project instructions",
                no_wrap=True,
                overflow="ellipsis",
            ),
            Text(note, style="dim", overflow="ellipsis", no_wrap=True),
            Rule(style=_ACCENT),
            Text("What's new", style=f"bold {_ACCENT}", no_wrap=True),
            *(Text(item, no_wrap=True, overflow="ellipsis") for item in items),
            Text("/release-notes for more", style="italic dim", no_wrap=True),
        )

    def input_rule(self) -> None:
        self.console.print(Rule(style="dim"))

    async def event(self, event: dict) -> None:
        record = dict(event)
        record.setdefault("received_at", time.time())
        self._events.append(record)
        self._events = self._events[-500:]
        kind = event.get("type")
        if kind == "content_delta":
            text = str(event.get("text", ""))
            if self._stream_started_at is None:
                self._stream_started_at = time.monotonic()
            self._stream_chars += len(text)
            self._streamed += text
            self.console.print(text, end="", markup=False, soft_wrap=True)
        elif kind == "thought":
            if self.verbose and not self._streamed:
                self.console.print(Panel(str(event.get("text", "")), title="reasoning", border_style="dim"))
        elif kind == "action":
            if self._streamed:
                self.console.print()
                self._streamed = ""
            args = json.dumps(event.get("args", {}), ensure_ascii=False)
            if len(args) > 180 and not self.verbose:
                args = args[:177] + "..."
            self.console.print(f"[yellow]●[/yellow] [bold]{event.get('name', '')}[/bold] [dim]{args}[/dim]")
        elif kind == "observation":
            text = str(event.get("text", ""))
            if len(text) > 400 and not self.verbose:
                text = text[:397] + "..."
            style = "red" if event.get("is_error") else "dim green"
            self.console.print(Text("  " + text, style=style), soft_wrap=True)
        elif kind == "result":
            text = str(event.get("text", ""))
            if self._stream_started_at is not None:
                elapsed = max(0.001, time.monotonic() - self._stream_started_at)
                self.last_output_speed = (self._stream_chars / 4) / elapsed
            if self._streamed:
                self.console.print()
                if self._streamed.strip() != text.strip():
                    self.console.print(Markdown(text))
            else:
                self.console.print(Markdown(text))
            self._streamed = ""
            self._stream_started_at = None
            self._stream_chars = 0

    def user(self, text: str, attachments=()) -> None:
        self.console.print("\n[bold cyan]>[/bold cyan] ", end="")
        self.console.print(text, markup=False)
        for index, attachment in enumerate(attachments, 1):
            label = f"Image #{index}" if attachment.kind == "image" else attachment.display_name
            self.console.print(f"  [magenta]▣[/magenta] {label} [dim]({attachment.kind})[/dim]")

    def show_transcript(self, messages: list[dict]) -> None:
        self.console.print(Panel.fit("Transcript", border_style=_ACCENT))
        for message in messages:
            role = str(message.get("role", "message"))
            content = self._message_preview(message.get("content", ""))
            color = "cyan" if role == "user" else (_ACCENT if role == "assistant" else "dim")
            self.console.print(Text(role.upper(), style=f"bold {color}"))
            self.console.print(Markdown(content) if content else Text("(empty)", style="dim"))
        actions = [event for event in self._events if event.get("type") in ("action", "observation")]
        if actions:
            self.console.print(Rule("tool activity", style="dim"))
            for event in actions[-30:]:
                if event.get("type") == "action":
                    self.console.print(f"[yellow]●[/yellow] {event.get('name', '')}")
                else:
                    self.console.print(Text("  " + str(event.get("text", ""))[:500], style="dim"))

    def show_release_notes(self) -> None:
        for release in RELEASES:
            self.console.print(Rule(f"cc-harness v{release.version}", style=_ACCENT))
            for item in release.items:
                self.console.print(f"[color(209)]•[/color(209)] {item}")

    def info(self, text: str) -> None:
        # Command output can contain arbitrary text (for example ``git diff``
        # hunks with ``[... ]``).  Render the payload as literal Text so Rich
        # markup cannot interpret or reject user/tool output.
        self.console.print(Text("● ", style="cyan") + Text(str(text)))

    def warning(self, text: str) -> None:
        self.console.print(Text("▲ ", style="yellow") + Text(str(text)))

    def error(self, text: str) -> None:
        self.console.print(Text("Error: ", style="red") + Text(str(text)))

    @staticmethod
    def git_branch(cwd: Path) -> str:
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd, stderr=subprocess.DEVNULL, text=True,
            ).strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                check=False,
            ).stdout.strip()
            return branch + ("*" if dirty else "")
        except (OSError, subprocess.SubprocessError):
            return ""

    @staticmethod
    def _message_preview(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "image_url":
                parts.append("[Image]")
        return "\n".join(parts)

    @staticmethod
    def _setting(runtime, name: str, default):
        return getattr(getattr(runtime, "terminal_settings", None), name, default)
