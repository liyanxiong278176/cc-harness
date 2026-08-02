"""ChatLog — middle chat area; 4-phase ReAct output (思考/行动/观察/结果)."""
from typing import Any

from rich.segment import Segment
from rich.text import Text
from textual.geometry import Size
from textual.strip import Strip
from textual.widgets import RichLog
from textual.widgets._rich_log import measure_renderables


class ChatLog(RichLog):
    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        padding: 1 2;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Task 17:diff-only todo state.
        self._todo_items: list[dict[str, str]] = []
        self._todo_line_idx: int | None = None
        self._todo_count: int = 0

    def write_user(self, text: str) -> None:
        self.write(f"\n[bold cyan]›[/bold cyan] {text}")

    def write_assistant_chunk(self, token: str) -> None:
        # 临时 static,完成后替换为 render
        self.write(token, expand=False)

    def write_assistant_final(self, text: str) -> None:
        self.write(f"\n[green]●[/green] {text}")

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.write(f"\n[yellow]●[/yellow] {name}({args})")

    def write_tool_result(self, result: str, error: bool) -> None:
        color = "red" if error else "green"
        self.write(f"  [{color}]{result}[/{color}]")

    def write_todo(self, items: list[dict[str, str]]) -> None:
        """Diff-only update: maintain a single todo block, replacing in-place.

        First call appends lines to the chat log and records the start index
        + item count. Subsequent calls replace the recorded slice in
        ``self.lines`` with freshly rendered strips so the todo block stays
        at the same position instead of duplicating on every update.
        """
        self._todo_items = items
        new_strips = [self._render_to_strip(self._format_todo_line(item)) for item in items]

        if self._todo_line_idx is None:
            # First call: append to log + record block position
            for strip in new_strips:
                self.lines.append(strip)
            self._todo_line_idx = len(self.lines) - len(new_strips)
            self._todo_count = len(new_strips)
            self._update_virtual_size()
        else:
            # Subsequent calls: replace the recorded slice in-place
            start = self._todo_line_idx
            end = start + self._todo_count
            self.lines[start:end] = new_strips
            self._todo_count = len(new_strips)
            self._update_virtual_size()

        self.refresh()

    @staticmethod
    def _format_todo_line(item: dict[str, str]) -> str:
        """Format a single todo item. The leading ``[`` is prefixed with
        ``\\`` so Rich's markup parser treats it as a literal bracket
        instead of opening a style tag (only the leading ``[`` needs
        escaping; the trailing ``]`` is opaque to the parser)."""
        mark = "x" if item.get("status") == "done" else " "
        return f"  - \\[{mark}] {item.get('title', '')}"

    def _render_to_strip(self, text: str) -> Strip:
        """Render a string into a single Strip — mirrors RichLog.write internals
        so we can splice the result into ``self.lines`` at an arbitrary index.

        Bypasses ``_make_renderable`` to skip the ``ReprHighlighter`` —
        ``highlight=True`` on the widget would otherwise bold the literal
        ``[`` / ``]`` checkbox brackets as "structural" chars and break
        the diff-only substring assertion in test_todo_diff.py.
        """
        console = self.app.console
        render_options = console.options
        if not self.wrap:
            render_options = render_options.update(overflow="ignore", no_wrap=True)

        # Build Text directly — markup via from_markup is still applied so
        # callers can use ``\\`` escapes, but the highlighter is skipped.
        renderable = Text.from_markup(text) if self.markup else Text(text)
        renderable_width = measure_renderables(console, render_options, [renderable]).maximum
        scrollable_content_width = self.scrollable_content_region.width
        render_width = max(renderable_width, scrollable_content_width)
        render_width = max(render_width, self.min_width)

        render_options = render_options.update_width(render_width)
        segments = console.render(renderable, render_options)
        lines = list(Segment.split_lines(segments))

        if not lines:
            return Strip.blank(render_width)

        strips = Strip.from_lines(lines)
        for strip in strips:
            strip.adjust_cell_length(render_width)

        # Todo lines are single-line; collapse multi-line defensively.
        return strips[0]

    def _update_virtual_size(self) -> None:
        """Recompute virtual_size after a manual lines mutation."""
        widest = max(
            (strip.cell_length for strip in self.lines),
            default=0,
        )
        self._widest_line_width = max(self._widest_line_width, widest)
        self.virtual_size = Size(self._widest_line_width, len(self.lines))