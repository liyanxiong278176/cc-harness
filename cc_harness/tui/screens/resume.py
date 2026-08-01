"""ResumeScreen — modal listing prior sessions, selection loads chosen one."""
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Header, OptionList
from textual.widgets._option_list import Option


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
        yield Vertical(
            Header(),
            OptionList(*items) if items else OptionList(Option("(no prior sessions)")),
            id="resume-modal",
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)