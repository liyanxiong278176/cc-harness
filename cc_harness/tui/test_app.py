"""Tests for cc_harness.tui.app — PipTuiApp composition + theme.

Task 7 acceptance tests.
"""
from cc_harness.tui.app import PipTuiApp


async def test_app_composes_four_widgets():
    """App 必须 mount 4 个 widget:Header / ChatLog / PromptInput / Footer。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # compose 后查询 DOM
        from textual.widgets import Static, TextArea, RichLog
        header = pilot.app.query_one("HeaderBar")
        chat = pilot.app.query_one("ChatLog")
        prompt = pilot.app.query_one("PromptInput")
        footer = pilot.app.query_one("FooterBar")
        assert header is not None
        assert chat is not None
        assert prompt is not None
        assert footer is not None


async def test_app_default_theme_is_tokyo_night():
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)):
        assert app.theme == "tokyo-night"