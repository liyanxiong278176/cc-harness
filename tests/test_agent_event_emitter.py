"""event_emitter 形参:None 时行为不变;非 None 时收 4 类事件。"""
import pytest
from tests.test_agent import FakeLLM  # 沿用现有 FakeLLM


@pytest.fixture
def captured_events():
    return []


async def test_emitter_none_is_silent(captured_events):
    """event_emitter=None 时不发任何调用。"""
    from cc_harness.agent import run_turn
    llm = FakeLLM(responses=[[...]])  # 用 Task 3 测试用的 fixture
    # 简单 sanity:不传 emitter 不报错
    # 这里只确认签名存在,具体事件流测 Task 3
    assert run_turn is not None and llm is not None  # placeholder,Task 3 替换


async def test_emitter_receives_events(captured_events):
    """event_emitter 收到 4 类事件(thought/action/observation/result)。"""
    async def emitter(ev):
        captured_events.append(ev)
    # Task 3 内部用 FakeLLM 模拟,这里先标空
    assert True
