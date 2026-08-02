"""ResumeScreen — modal listing prior sessions, selection loads chosen one."""
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Header, OptionList
from textual.widgets._option_list import Option


# v1 历史存储尚未 wire 到 cc_harness.storage;显式告知用户而不是假装空列表。
# 后续 task(19+)接 storage 后,这里会替换为真正的 session 列表。
_HISTORY_PLACEHOLDER = "(history not yet wired in v1)"


class ResumeScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self.sessions = sessions  # [{id, title, started_at, message_count}]

    def compose(self):
        items = [
            Option(
                f"{s.get('title', s['id'])} ({s.get('message_count', 0)} msgs)",
                id=s["id"],
            )
            for s in self.sessions
        ]
        if not items:
            items = [Option(_HISTORY_PLACEHOLDER, id="_placeholder", disabled=True)]
        yield Vertical(
            Header(),
            OptionList(*items),
            id="resume-modal",
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Placeholder entry is disabled — on_option_list_option_selected 仍可能触发,
        # 但 id 以 "_" 开头,直接 dismiss None 不打开任何 session。
        if event.option.id.startswith("_"):
            return
        self.dismiss(event.option.id)