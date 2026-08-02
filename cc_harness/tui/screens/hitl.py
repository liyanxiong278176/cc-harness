"""HITLScreen — Human-in-the-loop confirm modal(yes / always / no)。

L4 confirm 接入点:agent 在执行危险工具前调用 TUIDriver.ask_user(question),
该方法 push HITLScreen,await 用户选择(yes/always/no)后 dismiss 回回调。
"""
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RadioButton, RadioSet, Static


class HITLScreen(ModalScreen):
    """3-选 1 confirm modal:Yes / Always(this session)/ No。

    默认选 No(安全默认 — 用户必须主动切到 yes 才执行)。
    按 Enter 触发 Confirm Button → dismiss(选中的 radio id)。
    """

    BINDINGS = [
        ("escape", "dismiss_no", "No"),
        ("y", "yes", "Yes"),
        ("a", "always", "Always"),
        ("n", "dismiss_no", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self):
        yield Vertical(
            Static(self.question, id="hitl-question"),
            RadioSet(
                RadioButton("Yes", id="yes"),
                RadioButton("Always (this session)", id="always"),
                RadioButton("No", id="no", value=True),
                id="hitl-radios",
            ),
            Horizontal(
                Button("Confirm", id="confirm", variant="primary"),
                id="hitl-buttons",
            ),
            id="hitl-modal",
        )

    def on_mount(self) -> None:
        # Confirm 按钮默认 focus — 用户按 Enter 立即触发 dismiss,不需要先 Tab
        self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        rs = self.query_one("#hitl-radios", RadioSet)
        pressed = rs.pressed_button.id if rs.pressed_button else "no"
        self.dismiss(pressed)

    def action_yes(self) -> None:
        self.dismiss("yes")

    def action_always(self) -> None:
        self.dismiss("always")

    def action_dismiss_no(self) -> None:
        self.dismiss("no")
