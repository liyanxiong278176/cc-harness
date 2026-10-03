import asyncio
from cc_harness.memory.store import Memory
from cc_harness.memory.retriever import MemoryRetriever


class FakeStore:
    def __init__(self, results):
        self._results = results
        self.last_query = None
        self.last_k = None
    async def search_similar(self, query_embedding, k=5):
        self.last_query = query_embedding
        self.last_k = k
        return self._results


class FakeEmbedder:
    def __init__(self, vec): self._vec = vec
    async def embed(self, text): return self._vec
    async def embed_batch(self, texts): return [self._vec for _ in texts]


def _mem(id, text, source="llm", updated_at=0):
    return Memory(id=id, text=text, embedding=[0.1]*4,
                  created_at=0, updated_at=updated_at, source=source)


def test_empty_query_returns_empty():
    async def run():
        r = MemoryRetriever(FakeStore([]), FakeEmbedder([0.1]*4), top_k=5, token_budget=800)
        return await r.build_injection_block("")
    assert asyncio.run(run()) == ""


def test_no_memories_returns_empty():
    async def run():
        r = MemoryRetriever(FakeStore([]), FakeEmbedder([0.1]*4))
        return await r.build_injection_block("anything")
    assert asyncio.run(run()) == ""


def test_format_includes_text_and_source():
    results = [(_mem("a", "用户住北京", "llm"), 0.1)]
    async def run():
        r = MemoryRetriever(FakeStore(results), FakeEmbedder([0.1]*4))
        return await r.build_injection_block("where is user")
    out = asyncio.run(run())
    assert "用户住北京" in out
    assert "[源: llm" in out
    assert "相关记忆" in out


def test_token_budget_truncates():
    long = "x" * 200
    results = [(_mem(f"id{i}", long, "llm"), 0.1 * i) for i in range(5)]
    async def run():
        r = MemoryRetriever(FakeStore(results), FakeEmbedder([0.1]*4), top_k=5, token_budget=50)
        return await r.build_injection_block("query")
    out = asyncio.run(run())
    included = out.count("- " + "x" * 200)
    assert included <= 2
