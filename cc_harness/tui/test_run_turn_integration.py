"""Tests for cc_harness.tui.app — real run_turn integration with event_emitter.

Task 15 acceptance test:用户输入(非 slash)→ _handle_user_input calls
cc_harness.agent.run_turn 并注入 event_emitter(TUIDriver 适配器)。

Stub-only integration from Task 14 is REPLACED with real run_turn wiring;
fake_run_turn 验证 event_emitter 被正确传入(占位实现,避开真实 LLM 依赖)。
"""
import asyncio
from unittest.mock import patch

from cc_harness.tui.app import PipTuiApp


async def test_run_turn_called_with_event_emitter():
    """用户输入触发 run_turn,event_emitter 是被传入并触发的对象。"""
    app = PipTuiApp()
    captured = []

    async def fake_run_turn(messages, *, event_emitter=None):
        # 验证 call site 传入了非 None event_emitter + messages
        captured.append((messages, event_emitter))
        if event_emitter is not None:
            # 模拟 run_turn 内部向 emitter 投递一次事件(验证 emit 路径)
            await event_emitter({"type": "result", "text": "ok", "ts": 0.0})
        return None

    with patch("cc_harness.tui.app._run_turn", fake_run_turn):
        async with app.run_test(size=(120, 40)) as pilot:
            await app._handle_user_input("hello")
            await pilot.pause()
            await asyncio.sleep(0)  # let event_emitter scheduled work flush

    # fake_run_turn 被调一次
    assert len(captured) == 1
    messages, emitter = captured[0]
    # messages 是 app 内 OpenAI 风格 list
    assert isinstance(messages, list)
    # event_emitter 非 None(call site 真的传了)
    assert emitter is not None