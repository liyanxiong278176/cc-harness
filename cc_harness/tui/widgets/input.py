"""PromptInput — bottom text area for user input; bindings added in later tasks."""
from textual.widgets import TextArea


class PromptInput(TextArea):
    DEFAULT_CSS = """
    PromptInput {
        height: 3;
        border: solid $primary;
    }
    """

    BINDINGS = [
        # 后续 task 11 加 Ctrl+C / Ctrl+L / Ctrl+R / Shift+Tab / Ctrl+T
        ("tab", "complete", "Complete @ or /"),
    ]

    async def action_complete(self) -> None:
        # 后续 task 12 实现:@path / /command 补全
        pass
