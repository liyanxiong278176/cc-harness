"""Tests for cc_harness.tui.app — _handle_user_input + _run_turn_stub integration.

Task 14 acceptance test:用户输入(非 slash)→ user message 写进 chat + stub echo 写进 chat。
真实 run_turn wiring 在 Task 15 完成,本 task 只验证入口 + driver dispatch 路径。
"""
from cc_harness.tui.app import PipTuiApp


async def test_user_input_writes_user_message_and_stub_response():
    """用户输入(非 slash)→ user message 写进 chat + stub echo 写进 chat。"""
    app = PipTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app._handle_user_input("hello")
        await pilot.pause()
        chat = app.query_one("#chat")
        rendered = "\n".join(str(line) for line in chat.lines)
        assert "hello" in rendered
        assert "stub" in rendered
        assert "echo" in rendered
