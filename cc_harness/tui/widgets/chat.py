"""ChatLog — middle chat area; 4-phase ReAct output (思考/行动/观察/结果)."""
from typing import Any

from textual.widgets import RichLog


class ChatLog(RichLog):
    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        padding: 1 2;
    }
    """

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
        for item in items:
            mark = "x" if item.get("status") == "done" else " "
            title = item.get("title", "")
            self.write(f"  - [{mark}] {title}")