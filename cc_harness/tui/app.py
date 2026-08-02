"""PipTuiApp — cc-harness TUI main app, 4-zone layout, Claude Code style."""
import asyncio
import subprocess
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from cc_harness.tui.history import History
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
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+l", "clear_screen", "Clear"),
        Binding("ctrl+r", "search_history", "History"),
        Binding("shift+tab", "toggle_permission", "Permission"),
        Binding("ctrl+t", "toggle_todo", "Todo"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Status 字段在 on_status_write 时更新;task 10 加强 header 渲染
        self._status: dict[str, str] = {}
        # Task 11:键盘快捷键需要的状态
        self._interrupt_event = asyncio.Event()
        self._permission_mode = "default"
        self._history = History()
        # Task 14:run_turn 的 OpenAI 风格 messages 历史
        # 真实 wiring 在 Task 15 接 cc_harness.agent.run_turn
        self._messages: list[dict] = []

    def compose(self):
        yield HeaderBar(id="header")
        yield ChatLog(id="chat", highlight=True, markup=True, wrap=True)
        yield PromptInput(id="prompt")
        yield FooterBar(id="footer")

    async def on_mount(self) -> None:
        # 启动时刷新一次 status(model/cwd/branch/mode/permission)
        await self._refresh_status(
            model=self._detect_model(),
            cwd=str(Path.cwd()),
            branch=self._detect_branch(),
            mode="coding",
            permission="default",
        )

    # --- Status / token refresh(task 10) ---

    async def _refresh_status(
        self,
        *,
        model: str,
        cwd: str,
        branch: str,
        mode: str,
        permission: str,
    ) -> None:
        header = self.query_one("#header", HeaderBar)
        header.render_status(model, cwd, branch, mode, permission)

    async def _refresh_footer(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost: float,
    ) -> None:
        footer = self.query_one("#footer", FooterBar)
        footer.render_tokens(input_tokens, output_tokens, cost)

    def _detect_model(self) -> str:
        # v1 写死;后续接 LLMClient.config
        return "claude-opus-4"

    def _detect_branch(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(Path.cwd()),
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            return "no-git"

    def action_clear_screen(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        chat.clear()

    # --- Task 11:键盘快捷键 actions ---

    def action_interrupt(self) -> None:
        self._interrupt_event.set()

    async def action_search_history(self) -> None:
        # v1:先 focus 回 prompt,真正的反向搜索 UI 后续 task 升级
        self.query_one("#prompt", PromptInput).focus()

    def action_toggle_permission(self) -> None:
        self._permission_mode = "auto" if self._permission_mode == "default" else "default"
        # 刷新 header + 通过 RenderEvent 通知外部
        from cc_harness.render import emit
        from cc_harness.render_protocol import PermissionModeChanged
        from cc_harness.tui.driver import TUIDriver

        emit(PermissionModeChanged(mode=self._permission_mode), driver=TUIDriver(self))

    def action_toggle_todo(self) -> None:
        # 后续 task 17 实现
        pass

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

    async def on_token_refresh(self, message: TokenRefresh) -> None:
        # v1 cost = 0.0;后续 task 接 pricing model
        await self._refresh_footer(
            input_tokens=message.stats.input_tokens,
            output_tokens=message.stats.output_tokens,
            cost=0.0,
        )

    # --- Task 13:slash command dispatcher ---

    async def _handle_slash_command(self, cmd: str) -> None:
        """解析并分发 /开头的命令。未知命令写回 chat。"""
        cmd = cmd.strip()
        if not cmd.startswith("/"):
            return
        name = cmd.split()[0]
        if name == "/help":
            from cc_harness.tui.screens.help import HelpScreen
            await self.push_screen(HelpScreen())
        elif name == "/theme":
            from cc_harness.tui.screens.theme import ThemeScreen
            await self.push_screen(ThemeScreen())
        elif name == "/resume":
            from cc_harness.tui.screens.resume import ResumeScreen
            # v1:从 history.json 读历史(后续 task 19 接 storage)
            sessions = self._load_sessions()
            await self.push_screen(ResumeScreen(sessions))
        elif name == "/clear":
            self.action_clear_screen()
        elif name == "/exit":
            self.exit()
        else:
            # 未知命令 — 显示在 chat
            chat = self.query_one("#chat", ChatLog)
            chat.write(f"[red]Unknown command: {name}[/red]")

    def _load_sessions(self) -> list[dict]:
        # v1 stub:后续 task 19 接 cc_harness/storage
        return []

    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        """PromptInput 提交事件:统一走 _handle_user_input 派发。"""
        self.run_worker(self._handle_user_input(message.text))

    # --- Task 14:用户输入入口 + run_turn stub ---

    async def _handle_user_input(self, text: str) -> None:
        """用户输入入口:slash 命令走 dispatcher,普通文本写 user message + 调 run_turn。

        真实 run_turn wiring 在 Task 15 完成,本 task 只接 stub。
        """
        if not text.strip():
            return
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return
        chat = self.query_one("#chat", ChatLog)
        chat.write_user(text)
        # v1 stub:Task 15 接真 run_turn
        await self._run_turn_stub(text)

    async def _run_turn_stub(self, text: str) -> None:
        """v1 stub:写一个 FinalText 测试 _handle_user_input 的 end-to-end 路径。

        真实 run_turn wiring 在 Task 15 完成(届时会 append user message 到
        self._messages,调 LLM,把 FinalText 走 emit 派回 driver)。
        """
        from cc_harness.render import emit
        from cc_harness.render_protocol import FinalText
        from cc_harness.tui.driver import TUIDriver

        driver = TUIDriver(self)
        emit(FinalText(text=f"(stub) echo: {text}"), driver=driver)
