"""FooterBar — bottom token/cost/help line."""
from textual.widgets import Static


class FooterBar(Static):
    DEFAULT_CSS = """
    FooterBar {
        height: 1;
        dock: bottom;
        background: $boost;
        color: $text;
    }
    """

    def render_tokens(self, in_tok: int, out_tok: int, cost: float) -> None:
        self.update(f"↑ {in_tok} ↓ {out_tok} · ${cost:.4f}    │    ⓘ ? for help")
