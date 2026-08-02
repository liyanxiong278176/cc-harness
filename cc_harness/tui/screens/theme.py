"""ThemeScreen — modal for switching between built-in Textual themes."""
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Header, OptionList
from textual.widgets._option_list import Option

THEME_OPTIONS = [
    ("textual-dark", "Dark (default)"),
    ("textual-light", "Light"),
    ("system", "跟随系统"),
    ("high-contrast", "High Contrast"),
]


class ThemeScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self):
        yield Vertical(
            Header(),
            OptionList(*[Option(label, id=_id) for _id, label in THEME_OPTIONS]),
            id="theme-modal",
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        theme_id = event.option.id
        if theme_id == "system":
            # 系统主题跟随后续 task 升级
            self.app.theme = "textual-dark"
        else:
            self.app.theme = theme_id
        self.dismiss()