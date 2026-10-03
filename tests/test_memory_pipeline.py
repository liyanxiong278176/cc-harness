import asyncio
from cc_harness.memory.service import SaveResult
from cc_harness.memory.pipeline import MemoryPipeline, PipelineResult
from cc_harness.tokens import TokenCounter


class _FakeStreamEvent:
    def __init__(self, kind="content", text="", content="", finish_reason="stop", pending=None, usage=None):
        self.kind = kind
        self.text = text
        self.content = content
        self.finish_reason = finish_reason
        self.pending = pending or []
        self.usage = usage


class FakeLLM:
    """Returns given text on chat()."""
    def __init__(self, text):
        self.text = text
        self.calls = 0
    async def chat(self, messages, tools=None):
        self.calls += 1
        yield _FakeStreamEvent(kind="content", text=self.text)
        yield _FakeStreamEvent(kind="done", content=self.text, finish_reason="stop")


class FakeService:
    def __init__(self):
        self.saved = []
    async def save(self, text, source, session_id=None, *, turn_idx=None):
        self.saved.append((text, source))
        return SaveResult(action="ADD", duration_ms=1)


def _msg(role, text):
    return {"role": role, "content": text}


def test_pipeline_skips_below_threshold():
    svc = FakeService()
    llm = FakeLLM("ignored")
    counter = TokenCounter()
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    pipe = MemoryPipeline(llm, svc, threshold=0.55, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=10_000_000)
    result = asyncio.run(run())
    assert result is None
    assert svc.saved == []
    assert llm.calls == 0


def test_pipeline_runs_above_threshold():
    svc = FakeService()
    big = "x" * 1000
    msgs = [_msg("user", big) for _ in range(200)]
    counter = TokenCounter()
    llm = FakeLLM('{"memories": ["用户住北京", "用 ruff"]}')
    pipe = MemoryPipeline(llm, svc, threshold=0.5, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=1000)
    result = asyncio.run(run())
    assert isinstance(result, PipelineResult)
    assert llm.calls == 1
    assert len(svc.saved) == 2
    assert ("用户住北京", "pipeline") in svc.saved


def test_pipeline_extract_failure_isolated():
    class FailingLLM:
        async def chat(self, messages, tools=None):
            raise RuntimeError("LLM down")
            yield  # never reached
    svc = FakeService()
    counter = TokenCounter()
    big = "x" * 1000
    msgs = [_msg("user", big) for _ in range(200)]
    pipe = MemoryPipeline(FailingLLM(), svc, threshold=0.5, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=1000)
    result = asyncio.run(run())
    assert isinstance(result, PipelineResult)
    assert result.error is not None
    assert "RuntimeError" in result.error or "llm" in result.error.lower()
    assert svc.saved == []


def test_pipeline_single_save_failure_isolated():
    """一条候选 save 失败不影响其他。"""
    class HalfBrokenService:
        def __init__(self):
            self.call_count = 0

        async def save(self, text, source, session_id=None, *, turn_idx=None):
            self.call_count += 1
            if self.call_count == 2:
                return SaveResult(action="ERROR", error="boom")
            return SaveResult(action="ADD", duration_ms=1)
    svc = HalfBrokenService()
    counter = TokenCounter()
    big = "x" * 1000
    msgs = [_msg("user", big) for _ in range(200)]
    llm = FakeLLM('{"memories": ["a", "b", "c"]}')
    pipe = MemoryPipeline(llm, svc, threshold=0.5, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=1000)
    result = asyncio.run(run())
    assert svc.call_count == 3
    assert result is not None
    assert any(r.action == "ERROR" for r in result.results)
    assert any(r.action == "ADD" for r in result.results)
