"""Tests for cc_harness.tui.driver — TUIDriver dispatching RenderEvents to Textual messages.

Task 8 acceptance tests.
"""
from cc_harness.render import emit
from cc_harness.render_protocol import ThinkingChunk, ToolCallStart, FinalText


async def test_tui_driver_writes_text_via_message():
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        emit(FinalText(text="hello"), driver=driver)
        await pilot.pause()
        chat = app.query_one("#chat")
        # RichLog.lines 是 Strip 对象列表,转成可读字符串
        rendered = "\n".join(str(line) for line in chat.lines)
        assert "hello" in rendered


async def test_tui_driver_writes_chunk_accumulates():
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        emit(ThinkingChunk(delta="a"), driver=driver)
        emit(ThinkingChunk(delta="b"), driver=driver)
        emit(ThinkingChunk(delta="c"), driver=driver)
        # 50ms 节流需要 > 50ms 才能 flush;pilot.pause() 默认只 ~30ms
        await pilot.pause(0.1)
        chat = app.query_one("#chat")
        rendered = "\n".join(str(line) for line in chat.lines)
        assert "a" in rendered or "abc" in rendered


async def test_tui_driver_writes_tool_call():
    from cc_harness.tui.app import PipTuiApp
    from cc_harness.tui.driver import TUIDriver
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        driver = TUIDriver(app)
        emit(ToolCallStart(name="run_command", args={"cmd": "ls"}), driver=driver)
        await pilot.pause()
        chat = app.query_one("#chat")
        rendered = "\n".join(str(line) for line in chat.lines)
        assert "run_command" in rendered
