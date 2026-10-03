import asyncio
import pytest
from cc_harness.memory.store import Memory
from cc_harness.memory.decider import Decision, DecisionResult
from cc_harness.memory.service import MemoryService


# Fakes (deterministic, in-memory)
class FakeStore:
    def __init__(self):
        self.memories: dict[str, Memory] = {}
        self._counter = 0

    async def add(self, text, embedding, source, *, session_id=None):
        self._counter += 1
        m = Memory(
            id=f"id-{self._counter}", text=text, embedding=embedding,
            created_at=0, updated_at=0, source=source,
        )
        self.memories[m.id] = m
        return m

    async def update(self, id, text, embedding):
        m = self.memories[id]
        # Replace stored Memory with a new object so prior references (from get())
        # are not mutated. Mirrors MemoryStore.update's real behavior.
        new_m = Memory(
            id=m.id, text=text, embedding=embedding,
            created_at=m.created_at, updated_at=m.updated_at + 1, source=m.source,
        )
        self.memories[id] = new_m
        return new_m

    async def supersede(self, id, text, embedding, *, source=None, session_id=None):
        old = self.memories[id]
        old.validity = "superseded"
        new_m = Memory(
            id=f"id-{self._counter + 1}", text=text, embedding=embedding,
            created_at=old.created_at, updated_at=old.updated_at + 1,
            source=source or old.source, version=old.version + 1,
            supersedes_id=old.id,
        )
        self._counter += 1
        self.memories[new_m.id] = new_m
        return new_m

    async def delete(self, id):
        if id in self.memories:
            del self.memories[id]
            return True
        return False

    async def get(self, id):
        return self.memories.get(id)

    async def search_similar(self, query_embedding, k=5):
        # Return all stored memories with zero distance so the decider is consulted.
        return [(m, 0.0) for m in self.memories.values()][:k]

    async def search_reflections(self, limit=5, lookback_h=24):
        return []


class FakeEmbedder:
    """Deterministic embedding: same text -> same vec (all 0.1)."""
    async def embed(self, text):
        return [0.1] * 4

    async def embed_batch(self, texts):
        return [[0.1] * 4 for _ in texts]


class FakeDecider:
    def __init__(self, result):
        self._result = result
        self._llm = None

    async def decide(self, new_text, similar, *, recent_reflections=None):
        return self._result


@pytest.fixture
def svc():
    return MemoryService(
        store=FakeStore(),
        embedder=FakeEmbedder(),
        decider=FakeDecider(DecisionResult(action=Decision.ADD)),
    )


def test_save_no_similar_adds(svc):
    async def run():
        return await svc.save("用户住北京", source="llm")
    result = asyncio.run(run())
    assert result.action == "ADD"
    assert result.memory.text == "用户住北京"


def test_save_update_replaces_text(svc):
    async def setup():
        m = await svc.store.add("用户住北京", [0.1] * 4, "llm")
        return m.id
    mem_id = asyncio.run(setup())
    svc.decider = FakeDecider(DecisionResult(
        action=Decision.UPDATE, target_id=mem_id, merged_text="用户住北京朝阳区"))
    async def run():
        return await svc.save("用户住北京朝阳区", source="llm")
    result = asyncio.run(run())
    assert result.action == "UPDATE"
    assert result.memory.text == "用户住北京朝阳区"
    assert result.previous.text == "用户住北京"


def test_save_delete_then_add(svc):
    async def setup():
        m = await svc.store.add("old", [0.1] * 4, "llm")
        return m.id
    mem_id = asyncio.run(setup())
    svc.decider = FakeDecider(DecisionResult(action=Decision.DELETE, target_id=mem_id))
    async def run():
        return await svc.save("new conflicting fact", source="llm")
    result = asyncio.run(run())
    assert result.action == "DELETE_THEN_ADD"
    assert result.memory.text == "new conflicting fact"
    assert result.deleted_id == mem_id
    assert mem_id not in svc.store.memories


def test_save_noop_does_not_modify(svc):
    async def setup():
        m = await svc.store.add("x", [0.1] * 4, "llm")
        return m.id
    mem_id = asyncio.run(setup())
    svc.decider = FakeDecider(DecisionResult(action=Decision.NOOP))
    async def run():
        return await svc.save("x", source="llm")
    result = asyncio.run(run())
    assert result.action == "NOOP"
    assert mem_id in svc.store.memories


def test_save_embedding_error_returns_error_result():
    class BrokenEmbedder:
        async def embed(self, text):
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("boom")
    async def run():
        svc = MemoryService(
            store=FakeStore(),
            embedder=BrokenEmbedder(),
            decider=FakeDecider(DecisionResult(action=Decision.ADD)),
        )
        return await svc.save("x", source="llm")
    result = asyncio.run(run())
    assert result.action == "ERROR"
    assert "embedding" in (result.error or "")
