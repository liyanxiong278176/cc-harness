"""PipTuiApp — cc-harness TUI main app, 4-zone layout, Claude Code style."""
from textual.app import App
from textual.binding import Binding

from cc_harness.tui.widgets.header import HeaderBar
from cc_harness.tui.widgets.chat import ChatLog
from cc_harness.tui.widgets.input import PromptInput
from cc_harness.tui.widgets.footer import FooterBar
from cc_harness.tui.driver import (
    ChatWrite,
    TokenWrite,
    ToolCallWrite,
    ToolResultWrite,
    TodoWrite,
    StatusWrite,
    TokenRefresh,
)


class PipTuiApp(App):
    """cc-harness TUI 主应用,4-zone 布局,Claude Code 风格对齐。"""

    TITLE = "cc-harness"
    theme = "tokyo-night"

    BINDINGS = [
        # 后续 task 11 加 Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T / Tab
        Binding("ctrl+l", "clear_screen", "Clear"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Status 字段在 on_status_write 时更新;task 10 加强 header 渲染
        self._status: dict[str, str] = {}

    def compose(self):
        yield HeaderBar(id="header")
        yield ChatLog(id="chat", highlight=True, markup=True, wrap=True)
        yield PromptInput(id="prompt")
        yield FooterBar(id="footer")

    def action_clear_screen(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.clear()

    # --- RenderEvent → widget 派发(TUIDriver 走 post_message) ---

    def on_chat_write(self, message: ChatWrite) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.write_assistant_final(message.text)

    def on_token_write(self, message: TokenWrite) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.write_assistant_chunk(message.token)

    def on_tool_call_write(self, message: ToolCallWrite) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.write_tool_call(message.name, message.args)

    def on_tool_result_write(self, message: ToolResultWrite) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.write_tool_result(message.result, message.error)

    def on_todo_write(self, message: TodoWrite) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.write_todo(message.items)

    def on_status_write(self, message: StatusWrite) -> None:
        # Status 字段更新(后续 task 10 完整实现 header.render_status)
        self._status.update(message.fields)

    def on_token_refresh(self, message: TokenRefresh) -> None:
        footer = self.query_one("#footer", FooterBar)
        footer.render_tokens(
            in_tok=message.stats.input_tokens,
            out_tok=message.stats.output_tokens,
            cost=0.0,  # 后续 task 10 加 cost 计算
        )
