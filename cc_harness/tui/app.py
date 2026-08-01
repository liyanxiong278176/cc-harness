"""PipTuiApp — cc-harness TUI main app, 4-zone layout, Claude Code style."""
from textual.app import App
from textual.binding import Binding

from cc_harness.tui.widgets.header import HeaderBar
from cc_harness.tui.widgets.chat import ChatLog
from cc_harness.tui.widgets.input import PromptInput
from cc_harness.tui.widgets.footer import FooterBar


class PipTuiApp(App):
    """cc-harness TUI 主应用,4-zone 布局,Claude Code 风格对齐。"""

    TITLE = "cc-harness"
    theme = "tokyo-night"

    BINDINGS = [
        # 后续 task 11 加 Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T / Tab
        Binding("ctrl+l", "clear_screen", "Clear"),
    ]

    def compose(self):
        yield HeaderBar(id="header")
        yield ChatLog(id="chat", highlight=True, markup=True, wrap=True)
        yield PromptInput(id="prompt")
        yield FooterBar(id="footer")

    def action_clear_screen(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.clear()
