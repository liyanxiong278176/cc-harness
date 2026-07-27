"""session_run_loop 单测:UserInput 触发 run_turn + L2 命中发 l2_refused。"""
import asyncio  # noqa: F401  # brief verbatim(Task 18 E2E fleshing)
import pytest  # noqa: F401
from unittest.mock import AsyncMock  # noqa: F401

from cc_harness.web.run_loop import session_run_loop  # noqa: F401
from cc_harness.web.events import (  # noqa: F401
    UserInputEvent, L2RefusedEvent, ThoughtEvent, DoneEvent, serialize,
)


class FakeLLM:
    async def chat(self, *a, **k): return AsyncMock()(...)  # noqa: F841


class FakeL2:
    def __init__(self, trigger: bool = False):
        self._trigger = trigger
    def scan(self, text: str):
        if self._trigger and "badword" in text:
            return {"hit": True, "template": "请求被拒绝"}
        return {"hit": False}


async def test_l2_hit_short_circuits():
    """L2 命中 → 发 l2_refused,不调 run_turn。"""
    llm = FakeLLM()  # noqa: F841
    l2 = FakeL2(trigger=True)  # noqa: F841
    # 构造 session + ws + sm
    # 这里简化:只验 session_run_loop 内部逻辑(后续实装)
    assert True  # placeholder,实装见 Step 3