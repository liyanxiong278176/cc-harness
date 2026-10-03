import asyncio
from cc_harness.memory.decider import LLMDecider, Decision


# FakeLLM that returns pre-programmed text
class _FakeStreamEvent:
    def __init__(self, kind="content", text="", content="", finish_reason="stop", pending=None, usage=None):
        self.kind = kind
        self.text = text
        self.content = content
        self.finish_reason = finish_reason
        self.pending = pending or []
        self.usage = usage


class FakeLLM:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.last_messages = None
    async def chat(self, messages, tools=None):
        self.last_messages = messages
        yield _FakeStreamEvent(kind="content", text=self.response_text)
        yield _FakeStreamEvent(kind="done", content=self.response_text, finish_reason="stop")


def _mem(id="abc", text="old"):
    from cc_harness.memory.store import Memory
    return Memory(id=id, text=text, embedding=[0.1]*4, created_at=0, updated_at=0, source="llm")


def test_decide_no_similar_returns_add_without_calling_llm():
    llm = FakeLLM('{"action": "ADD"}')
    async def run():
        d = LLMDecider(llm)
        return await d.decide("new text", [])
    result = asyncio.run(run())
    assert result.action == Decision.ADD
    assert llm.last_messages is None  # 不调 LLM


def test_decide_update_parses_merged_text():
    llm = FakeLLM('{"action":"UPDATE","target_id":"abc","merged_text":"merged","reasoning":"r"}')
    async def run():
        return await LLMDecider(llm).decide("new", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.UPDATE
    assert result.target_id == "abc"
    assert result.merged_text == "merged"


def test_decide_delete():
    llm = FakeLLM('{"action":"DELETE","target_id":"abc"}')
    async def run():
        return await LLMDecider(llm).decide("new", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.DELETE
    assert result.target_id == "abc"


def test_decide_noop():
    llm = FakeLLM('{"action":"NOOP","reasoning":"same"}')
    async def run():
        return await LLMDecider(llm).decide("new", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.NOOP


def test_decide_parse_error_falls_back_to_noop():
    llm = FakeLLM("not a JSON at all")
    async def run():
        return await LLMDecider(llm).decide("x", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.NOOP
    assert "parse" in (result.error or "").lower() or result.error is not None


def test_decide_missing_merged_text_for_update_falls_back_to_noop():
    llm = FakeLLM('{"action":"UPDATE","target_id":"abc"}')  # 缺 merged_text
    async def run():
        return await LLMDecider(llm).decide("x", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.NOOP
    assert result.error is not None
