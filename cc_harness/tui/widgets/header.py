"""HeaderBar — top status row showing model / cwd / branch / mode / permission."""
from textual.widgets import Static


class HeaderBar(Static):
    DEFAULT_CSS = """
    HeaderBar {
        height: 1;
        dock: top;
        background: $boost;
        color: $text;
    }
    """

    def render_status(self, model: str, cwd: str, branch: str, mode: str, permission: str) -> None:
        self.update(f"{model} · {cwd} · {branch} · [{mode}] · [{permission}]")
