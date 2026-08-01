"""HelpScreen — modal showing keyboard shortcuts and slash commands."""
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

HELP_TEXT = """
cc-harness TUI 帮助

基础:
  Enter              提交输入
  Shift+Enter        换行
  Tab                补全 / 或 @
  ↑ / ↓              历史
  Ctrl+R             反向搜历史

快捷键:
  Ctrl+C             中断当前 LLM 流
  Ctrl+L             清屏
  Shift+Tab          切换权限模式 (default / auto)
  Ctrl+T             切换 todo 显示

命令:
  /help              本帮助
  /theme             切换主题
  /resume            历史 session
  /model             切换模型
  /clear             清空 chat
  /exit              退出

完整命令见 README。
"""


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self):
        yield Vertical(
            Static(HELP_TEXT, id="help-text"),
            Button("Close", id="close"),
            id="help-modal",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()