import asyncio
from cc_harness.memory.tools import memory_recall_handler, memory_save_handler
from cc_harness.memory.service import SaveResult
from cc_harness.memory.store import Memory


class FakeRetriever:
    def __init__(self, results=None, raise_exc=False):
        self._results = results or []
        self._raise = raise_exc
    async def search(self, query, top_k=5):
        if self._raise:
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("test boom")
        return self._results


class FakeService:
    def __init__(self, raise_exc=False, result=None):
        self._raise = raise_exc
        self._result = result or SaveResult(action="ADD",
                                              memory=Memory(id="x", text="t", embedding=[0.1]*4,
                                                            created_at=0, updated_at=0, source="llm"),
                                              duration_ms=5)
    async def save(self, text, source):
        if self._raise:
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("test boom")
        return self._result


def test_recall_empty_query_errors():
    async def run():
        return await memory_recall_handler({"query": ""}, cwd=".", retriever=FakeRetriever())
    r = asyncio.run(run())
    assert r.is_error
    assert "query" in r.llm_text


def test_recall_success_returns_formatted():
    results = [(Memory(id="a", text="用户住北京", embedding=[0.1]*4,
                        created_at=0, updated_at=0, source="llm"), 0.1)]
    async def run():
        return await memory_recall_handler({"query": "where"}, cwd=".", retriever=FakeRetriever(results))
    r = asyncio.run(run())
    assert not r.is_error
    assert "用户住北京" in r.llm_text


def test_recall_embedding_error_returns_tool_error():
    async def run():
        return await memory_recall_handler({"query": "x"}, cwd=".", retriever=FakeRetriever(raise_exc=True))
    r = asyncio.run(run())
    assert r.is_error
    assert "embedding" in r.llm_text.lower()


def test_save_success_returns_action():
    async def run():
        return await memory_save_handler({"text": "用户住北京"}, cwd=".", service=FakeService())
    r = asyncio.run(run())
    assert not r.is_error
    assert "ADD" in r.llm_text


def test_save_empty_text_errors():
    async def run():
        return await memory_save_handler({"text": ""}, cwd=".", service=FakeService())
    r = asyncio.run(run())
    assert r.is_error
    assert "text" in r.llm_text
