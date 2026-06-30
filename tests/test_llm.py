from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cc_harness.llm import LLMClient, PendingToolCall, accumulate_delta

def test_pending_tool_call_index_optional():
    p = PendingToolCall()
    assert p.index is None
    assert p.id is None
    assert p.name is None
    assert p.arguments_json == ""

def test_accumulate_delta_aligns_by_index():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=2, id="c1", name="t1", arguments_json='{"a":')
    assert len(pending) == 3
    assert pending[2].id == "c1"
    assert pending[2].name == "t1"
    assert pending[2].arguments_json == '{"a":'

def test_accumulate_delta_appends_when_index_none():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=None, id="c1", name="t1", arguments_json='{')
    accumulate_delta(pending, index=None, id="c2", name="t2", arguments_json='{')
    assert len(pending) == 2
    assert pending[0].id == "c1"
    assert pending[1].id == "c2"

def test_accumulate_delta_concat_arguments():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=0, id="c1", name="t1", arguments_json='{"a":')
    accumulate_delta(pending, index=0, id=None, name=None, arguments_json=' 1}')
    assert pending[0].arguments_json == '{"a": 1}'

class _FakeChoiceDelta:
    def __init__(self, content=None, tool_calls=None, reasoning=None):
        self.content = content
        self.tool_calls = tool_calls
        # DeepSeek reasoning models emit delta.reasoning_content separately.
        self.reasoning_content = reasoning

class _FakeChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason

class _FakeChunk:
    def __init__(self, delta, finish_reason=None):
        self.choices = [_FakeChoice(delta, finish_reason)]

def _tc(index, id_, name, arguments):
    """Build a fake tool_call delta. Use SimpleNamespace (NOT MagicMock) because
    `name` is the third positional/keyword arg of MagicMock.__init__ and gets
    consumed as the mock's repr-name, not an attribute. tc.name would then
    return a child MagicMock (truthy), not None."""
    return SimpleNamespace(
        index=index, id=id_, name=name,
        function=SimpleNamespace(arguments=arguments),
    )

def _make_client(stream_chunks):
    """Build an LLMClient whose underlying openai client yields stream_chunks."""
    client = LLMClient(api_key="sk-test", model="gpt-4o-mini", base_url=None)
    # Replace the internal async client
    mock = MagicMock()
    mock.chat.completions.create = AsyncMock(return_value=aiter(stream_chunks))
    client._client = mock
    return client

async def aiter(items):
    for x in items:
        yield x

@pytest.mark.asyncio
async def test_chat_streams_content_and_tool_calls():
    chunks = [
        _FakeChunk(_FakeChoiceDelta(content="I will ")),
        _FakeChunk(_FakeChoiceDelta(content="read the file")),
        _FakeChunk(_FakeChoiceDelta(tool_calls=[_tc(0, "c1", "t1", '{"pa')])),
        _FakeChunk(_FakeChoiceDelta(tool_calls=[_tc(0, None, None, 'th":1}')])),
        _FakeChunk(_FakeChoiceDelta(), finish_reason="tool_calls"),
    ]
    client = _make_client(chunks)
    events = []
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "hi"}], tools=[]):
        events.append(ev)
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.finish_reason == "tool_calls"
    assert final.content == "I will read the file"
    assert len(final.pending) == 1
    assert final.pending[0].name == "t1"
    assert final.pending[0].arguments_json == '{"path":1}'  # concatenated '{"pa' + 'th":1}'

@pytest.mark.asyncio
async def test_chat_bad_json_finishes_with_raw_arguments():
    """If a tool_call's concatenated arguments_json doesn't parse, the pending
    entry is left as-is (no exception). Caller (agent.py) will detect this."""
    chunks = [
        _FakeChunk(_FakeChoiceDelta(tool_calls=[_tc(0, "c1", "t1", '{"a": oops')])),
        _FakeChunk(_FakeChoiceDelta(), finish_reason="tool_calls"),
    ]
    client = _make_client(chunks)
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "x"}], tools=[]):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.pending[0].arguments_json == '{"a": oops'  # not parsed yet


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens

class _FakeUsageChunk:
    """The final stream chunk that carries usage (choices=[])."""
    def __init__(self, usage):
        self.choices = []
        self.usage = usage


@pytest.mark.asyncio
async def test_chat_captures_usage_on_final_chunk():
    """When the stream ends with a usage chunk, StreamEvent.usage should be set."""
    chunks = [
        _FakeChunk(_FakeChoiceDelta(content="hi"), finish_reason="stop"),
        _FakeUsageChunk(_FakeUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)),
    ]
    client = _make_client(chunks)
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "hi"}], tools=[]):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.usage is not None
    assert final.usage.prompt_tokens == 100
    assert final.usage.completion_tokens == 50
    assert final.usage.total_tokens == 150


@pytest.mark.asyncio
async def test_chat_usage_none_when_no_usage_chunk():
    """When no usage chunk is present, StreamEvent.usage should be None."""
    chunks = [
        _FakeChunk(_FakeChoiceDelta(content="ok"), finish_reason="stop"),
    ]
    client = _make_client(chunks)
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "hi"}], tools=[]):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.usage is None


@pytest.mark.asyncio
async def test_chat_reasoning_content_used_when_content_empty():
    """DeepSeek reasoning models (e.g. deepseek-v4-flash) sometimes emit the
    whole answer in delta.reasoning_content with empty delta.content. Without
    a fallback the turn looks 'empty' and the agent gives up. The done event
    must surface reasoning_content as content in that case."""
    chunks = [
        _FakeChunk(_FakeChoiceDelta(reasoning="你好！我是 cc-harness")),
        _FakeChunk(_FakeChoiceDelta(reasoning="，一个编程代理")),
        _FakeChunk(_FakeChoiceDelta(), finish_reason="stop"),
        _FakeUsageChunk(_FakeUsage(100, 50, 150)),
    ]
    client = _make_client(chunks)
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "hello"}], tools=[]):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.content == "你好！我是 cc-harness，一个编程代理"


@pytest.mark.asyncio
async def test_chat_content_preferred_over_reasoning_when_both_present():
    """When both content and reasoning_content are present, content wins
    (reasoning is internal thinking and must not pollute the answer)."""
    chunks = [
        _FakeChunk(_FakeChoiceDelta(reasoning="(internal thinking...)", content="ANSWER")),
        _FakeChunk(_FakeChoiceDelta(), finish_reason="stop"),
    ]
    client = _make_client(chunks)
    final = None
    async for ev in client.chat(messages=[{"role": "user", "content": "x"}], tools=[]):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.content == "ANSWER"
