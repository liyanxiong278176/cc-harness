"""Tests for cc_harness.tui.app — real run_turn integration with event_emitter.

Task 15 acceptance test:用户输入(非 slash)→ _handle_user_input calls
cc_harness.agent.run_turn 并注入 event_emitter(TUIDriver 适配器)。

Fix round 1:real cc_harness.agent.run_turn emits DICT events with `type`
field(`thought` / `action` / `observation` / `result`),and the call site
must pass `llm` + `mcp` as required positional args. Test exercises both.
"""
from unittest.mock import patch

from cc_harness.tui.app import PipTuiApp


async def test_run_turn_called_with_dict_event_emitter():
    """用户输入触发 run_turn,event_emitter 收到 dict event 并路由到 TUIDriver。"""
    app = PipTuiApp()
    captured = []

    async def fake_run_turn(messages, llm, mcp, *, event_emitter=None):
        captured.append((messages, llm, mcp, event_emitter))
        if event_emitter:
            # Real dict event shapes from cc_harness.agent.run_turn
            await event_emitter({"type": "thought", "text": "thinking...", "ts": 0.0, "iteration": 0})
            await event_emitter({"type": "action", "name": "run_command", "args": {"cmd": "ls"}, "ts": 0.0, "iteration": 0})
            await event_emitter({"type": "observation", "text": "file1", "is_error": False, "duration_ms": 100, "iteration": 0})
            await event_emitter({"type": "result", "text": "summary", "ts": 0.0})
        return None

    with patch("cc_harness.tui.app._run_turn", fake_run_turn):
        async with app.run_test(size=(120, 40)) as pilot:
            await app._handle_user_input("hello")
            await pilot.pause(0.1)
            # Verify run_turn called with llm + mcp + emitter
            assert len(captured) == 1
            messages, llm, mcp, emitter = captured[0]
            assert llm is None
            assert mcp is None
            assert emitter is not None
            # Verify message was appended BEFORE run_turn(OpenAI 风格顺序)
            assert len(messages) >= 1
            assert messages[-1]["role"] == "user"
            assert messages[-1]["content"] == "hello"