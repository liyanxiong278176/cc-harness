"""Fake-component end-to-end test for MemoryService + MemoryRetriever + MemoryPipeline."""
import pytest
from cc_harness.memory.store import MemoryStore
from cc_harness.memory.service import MemoryService
from cc_harness.memory.pipeline import MemoryPipeline
from cc_harness.memory.retriever import MemoryRetriever
from cc_harness.tokens import TokenCounter
from tests.test_memory_decider import FakeLLM


class FakeEmbedder:
    """确定性:同 text → 同 vec(用 hash + 取字节)"""
    def __init__(self):
        self.vectors = {}
    async def embed(self, text):
        if text in self.vectors:
            return self.vectors[text]
        h = hash(text)
        v = [float((h >> i) & 0xFF) / 255.0 for i in range(0, 32, 8)]
        v = (v + [0.0] * 4)[:4]
        self.vectors[text] = v
        return v
    async def embed_batch(self, texts):
        return [await self.embed(t) for t in texts]


class FakeDecider:
    """有相似走 UPDATE,没有相似走 ADD。"""
    _llm = None

    async def decide(self, new_text, similar, *, recent_reflections=None):
        from cc_harness.memory.decider import Decision, DecisionResult
        if not similar:
            return DecisionResult(action=Decision.ADD)
        target = similar[0][0]
        return DecisionResult(action=Decision.UPDATE, target_id=target.id,
                              merged_text=new_text)


@pytest.fixture
async def stack():
    """构造 in-memory 完整组件栈。"""
    embedder = FakeEmbedder()
    decider = FakeDecider()
    store = MemoryStore(db_path=":memory:", embedding_dim=4)
    await store.init_schema()
    service = MemoryService(store, embedder, decider)
    retriever = MemoryRetriever(store, embedder, top_k=5, token_budget=800)
    yield store, service, retriever, embedder, decider
    await store.close()


async def test_save_recall_round_trip(stack):
    store, service, retriever, _, _ = stack
    # 1. save
    r1 = await service.save("用户住北京", source="llm")
    assert r1.action == "ADD"
    # 2. save 相关(走 UPDATE 路径)
    r2 = await service.save("用户住北京朝阳区", source="llm")
    assert r2.action == "UPDATE"
    # 3. recall
    results = await retriever.search("where is user", top_k=5)
    assert any("用户住北京朝阳区" == m.text for m, _ in results)
    # 4. injection block
    block = await retriever.build_injection_block("where is user")
    assert "用户住北京朝阳区" in block


async def test_pipeline_extracts_and_saves_via_service(stack):
    store, service, retriever, _, _ = stack
    llm = FakeLLM('{"memories": ["项目用 ruff lint", "用户住北京"]}')
    pipe = MemoryPipeline(llm, service, threshold=0.5, recent_turns=10)
    big = "x" * 2000
    msgs = [{"role": "user", "content": big} for _ in range(50)]
    result = await pipe.maybe_run(msgs, TokenCounter(), context_window=1000)
    assert result is not None
    saved_texts = [getattr(r.memory, "text", "") for r in result.results]
    assert "项目用 ruff lint" in saved_texts
    assert "用户住北京" in saved_texts
    await store.close()


async def test_pipeline_skips_tagged_memory_messages(stack):
    _, _, _, _, _ = stack
    pipeline = MemoryPipeline(None, None)
    msgs = [
        {"role": "system", "content": "real system"},
        {"role": "system", "content": "injected memory", "_memory_block": True},
        {"role": "user", "content": "hi"},
    ]
    assert pipeline._recent_turns(msgs) == [msgs[2]]
