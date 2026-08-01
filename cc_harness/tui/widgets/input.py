"""PromptInput — bottom text area for user input; bindings added in later tasks."""
from textual.widgets import TextArea

from cc_harness.tui.completer import Completer


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

    def __init__(self, cwd: str = ".", **kwargs) -> None:
        super().__init__(**kwargs)
        self.completer = Completer(cwd=cwd)

    async def action_complete(self) -> None:
        """Tab 触发:找最近一个 / 或 @,用首条候选替换其右半边。

        v1:取首条替换(完整补全 popup 后续 task)
        """
        text = self.text
        # 倒扫:首个位于词首的 / 或 @
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if ch not in ("/", "@"):
                continue
            at_word_start = i == 0 or text[i - 1].isspace()
            if not at_word_start:
                continue
            prefix = text[i:]
            matches = self.completer.complete(prefix)
            if matches:
                self.text = text[:i] + matches[0]
            return