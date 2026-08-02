"""Tests for cc_harness.tui status/header/footer refresh.

Task 10 acceptance tests:
- Header 5 段:model / cwd / branch / mode / permission
- Footer token/cost:input tokens / output tokens / cost
"""
from cc_harness.tui.app import PipTuiApp


async def test_header_shows_5_segments():
    """Header 必须能渲染 5 段:model / cwd / branch / mode / permission。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        header = app.query_one("#header")
        await pilot.app._refresh_status(
            model="claude-opus-4",
            cwd="/tmp",
            branch="main",
            mode="coding",
            permission="default",
        )
        await pilot.pause()
        text = str(header.render())
        assert "claude-opus-4" in text
        assert "/tmp" in text
        assert "main" in text
        assert "coding" in text
        assert "default" in text


async def test_footer_shows_tokens_and_cost():
    """Footer 必须能渲染 input/output tokens 和 cost 估算。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        footer = app.query_one("#footer")
        await pilot.app._refresh_footer(input_tokens=1200, output_tokens=800, cost=0.04)
        await pilot.pause()
        text = str(footer.render())
        assert "1200" in text
        assert "800" in text
        assert "0.04" in text
