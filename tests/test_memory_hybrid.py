"""Phase 4 Q1 uplift: FTS5 关键词索引 + hybrid search 混合召回。

覆盖:
- FTS5 探测:SQLite 编译含 FTS5 → has_fts5=True
- FTS5 触发器同步:INSERT/UPDATE/DELETE 同步到 memories_fts
- search_fts:BM25 关键词召回
- search_hybrid:vec + fts RRF 合并,关键词命中时召回向量不命中的记忆
- FTS5 不可用时降级 vector-only(向后兼容)
- memory_recall_handler 用 hybrid(已有 test_memory_recall_retry 不破)
"""
from __future__ import annotations
import asyncio
import os
import pytest


# --- FTS5 schema + triggers ---

@pytest.mark.asyncio
async def test_fts5_probe_detects_availability(tmp_path):
    """MemoryStore.init_schema 探测 SQLite FTS5 是否可用。"""
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"fts.db", embedding_dim=4)
    await s.init_schema()
    # 探测结果取决于 Python sqlite3 编译 — 大多数含 FTS5
    # 仅断言属性存在(可能是 True 或 False),不强制
    assert isinstance(s.has_fts5, bool)
    await s.close()


@pytest.mark.asyncio
async def test_fts5_triggers_sync_on_insert(tmp_path):
    """INSERT memories → FTS5 自动有行。"""
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"ftsi.db", embedding_dim=4)
    await s.init_schema()
    if not s.has_fts5:
        pytest.skip("FTS5 not available in this Python build")
    mem = await s.add("Caroline went to LGBTQ support group", [0.1]*4, source="llm")
    # FTS5 命中 'LGBTQ' 应能搜到
    results = await s.search_fts("LGBTQ", k=5)
    assert len(results) == 1
    assert results[0][0].id == mem.id
    await s.close()


@pytest.mark.asyncio
async def test_fts5_triggers_sync_on_update(tmp_path):
    """UPDATE memories → FTS5 内容同步。"""
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"ftsu.db", embedding_dim=4)
    await s.init_schema()
    if not s.has_fts5:
        pytest.skip("FTS5 not available in this Python build")
    mem = await s.add("old text about Melanie", [0.1]*4, source="llm")
    assert len(await s.search_fts("Melanie", k=5)) == 1
    # 改成不含 Melanie 的文本
    await s.update(mem.id, "new text about Caroline", [0.2]*4)
    assert len(await s.search_fts("Melanie", k=5)) == 0
    assert len(await s.search_fts("Caroline", k=5)) == 1
    await s.close()


@pytest.mark.asyncio
async def test_fts5_triggers_sync_on_delete(tmp_path):
    """DELETE memories → FTS5 行消失。"""
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"ftsd.db", embedding_dim=4)
    await s.init_schema()
    if not s.has_fts5:
        pytest.skip("FTS5 not available in this Python build")
    mem = await s.add("unique_token_xyz123 test", [0.1]*4, source="llm")
    assert len(await s.search_fts("unique_token_xyz123", k=5)) == 1
    await s.delete(mem.id)
    assert len(await s.search_fts("unique_token_xyz123", k=5)) == 0
    await s.close()


@pytest.mark.asyncio
async def test_search_fts_empty_or_no_match(tmp_path):
    """空 query / 无命中返 []。"""
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"ftse.db", embedding_dim=4)
    await s.init_schema()
    if not s.has_fts5:
        pytest.skip("FTS5 not available")
    await s.add("some text", [0.1]*4, source="llm")
    assert await s.search_fts("", k=5) == []
    assert await s.search_fts("   ", k=5) == []
    assert await s.search_fts("nonexistent_token_qqq", k=5) == []
    await s.close()


# --- search_hybrid (RRF merge) ---

class _VecOnlyFakeStore:
    """Simulates store with FTS5 disabled."""
    has_fts5 = False
    def __init__(self, vec_results):
        self._vec = vec_results

    async def search_similar(self, embedding, k=5):
        return self._vec

    async def search_fts(self, query, k=5):
        return []


class _HybridFakeStore:
    """Simulates store with FTS5 enabled — separate vec/fts results."""
    has_fts5 = True
    def __init__(self, vec_results, fts_results):
        self._vec = vec_results
        self._fts = fts_results

    async def search_similar(self, embedding, k=5):
        return self._vec

    async def search_fts(self, query, k=5):
        return self._fts


class _FakeEmbedder:
    def __init__(self, vec=None):
        self._vec = vec or [0.0] * 4
    async def embed(self, text):
        return self._vec


class _FakeMemory:
    def __init__(self, mid, text):
        self.id = mid
        self.text = text
        self.source = "pipeline"
        self.layer = "L1"
        self.session_id = None
        self.created_at = 0.0
        self.updated_at = 0.0
        self.embedding = [0.0] * 4


@pytest.mark.asyncio
async def test_search_hybrid_merges_vec_and_fts_with_rrf():
    """RRF 合并:vec 和 fts 各自的 rank 加权算分。"""
    from cc_harness.memory.retriever import MemoryRetriever
    mem_a = _FakeMemory("a", "shared")
    mem_b = _FakeMemory("b", "vec-only")
    mem_c = _FakeMemory("c", "fts-only")
    store = _HybridFakeStore(
        vec_results=[(mem_a, 0.1), (mem_b, 0.5)],     # a, b
        fts_results=[(mem_a, 1.0), (mem_c, 2.0)],     # a, c
    )
    r = MemoryRetriever(store=store, embedder=_FakeEmbedder())
    results = await r.search_hybrid("query", top_k=5, alpha=0.5)
    ids = [m.id for m, _ in results]
    # a 出现在两路 → 合并分最高,排第 1
    assert ids[0] == "a"
    # b 和 c 各自只在一边,排后
    assert set(ids[1:]) == {"b", "c"}


@pytest.mark.asyncio
async def test_search_hybrid_recovers_keyword_only_match():
    """关键词命中但向量不命中时,hybrid 仍能召回。"""
    from cc_harness.memory.retriever import MemoryRetriever
    mem = _FakeMemory("kw1", "specific_keyword_xyz")
    store = _HybridFakeStore(
        vec_results=[],  # 向量无命中
        fts_results=[(mem, 0.5)],  # FTS 命中
    )
    r = MemoryRetriever(store=store, embedder=_FakeEmbedder())
    results = await r.search_hybrid("specific_keyword_xyz", top_k=5, alpha=0.5)
    assert len(results) == 1
    assert results[0][0].id == "kw1"


@pytest.mark.asyncio
async def test_search_hybrid_falls_back_when_no_fts5():
    """FTS5 不可用 → 只用 vec,不应崩。"""
    from cc_harness.memory.retriever import MemoryRetriever
    mem = _FakeMemory("v1", "vec hit")
    store = _VecOnlyFakeStore(vec_results=[(mem, 0.3)])
    r = MemoryRetriever(store=store, embedder=_FakeEmbedder())
    results = await r.search_hybrid("anything", top_k=5, alpha=0.5)
    assert len(results) == 1
    assert results[0][0].id == "v1"


@pytest.mark.asyncio
async def test_search_hybrid_alpha_extremes():
    """alpha=0 → 纯 FTS rank;alpha=1 → 纯 vec rank。"""
    from cc_harness.memory.retriever import MemoryRetriever
    mem_v = _FakeMemory("v", "vec only")
    mem_f = _FakeMemory("f", "fts only")
    store = _HybridFakeStore(
        vec_results=[(mem_v, 0.1)],
        fts_results=[(mem_f, 0.5)],
    )
    r = MemoryRetriever(store=store, embedder=_FakeEmbedder())
    # alpha=0 纯 FTS → f 第一
    res0 = await r.search_hybrid("q", top_k=5, alpha=0.0)
    assert res0[0][0].id == "f"
    # alpha=1 纯 vec → v 第一
    res1 = await r.search_hybrid("q", top_k=5, alpha=1.0)
    assert res1[0][0].id == "v"


# --- handler integration: 走 hybrid 路径 ---

@pytest.mark.asyncio
async def test_handler_uses_search_hybrid_when_available(monkeypatch):
    """retriever 有 search_hybrid → handler 调它,不再调 search。"""
    from cc_harness.memory import tools as tools_mod
    monkeypatch.setenv("MAX_RECALL_RETRIES", "0")  # 关闭 retry,只看路由
    import importlib
    importlib.reload(tools_mod)

    class _HybridAwareRetriever:
        def __init__(self):
            self.search_called = False
            self.hybrid_called = False
        async def search(self, *a, **k):
            self.search_called = True
            return []
        async def search_hybrid(self, *a, **k):
            self.hybrid_called = True
            return [(_FakeMemory("h1", "hybrid hit"), 0.1)]

    r = _HybridAwareRetriever()
    result = await tools_mod.memory_recall_handler(
        {"query": "test"}, cwd="/x", retriever=r
    )
    assert not result.is_error
    assert r.hybrid_called, "handler should call search_hybrid when available"
    assert not r.search_called, "handler should NOT fall back to search"
    assert "hybrid hit" in result.display_text


@pytest.mark.asyncio
async def test_handler_falls_back_to_search_when_no_hybrid(monkeypatch):
    """retriever 只有 search(无 search_hybrid) → handler 调 search 不崩。"""
    from cc_harness.memory import tools as tools_mod
    monkeypatch.setenv("MAX_RECALL_RETRIES", "0")
    import importlib
    importlib.reload(tools_mod)

    class _LegacyRetriever:
        def __init__(self):
            self.search_called = False
        async def search(self, *a, **k):
            self.search_called = True
            return [(_FakeMemory("l1", "legacy hit"), 0.2)]

    r = _LegacyRetriever()
    result = await tools_mod.memory_recall_handler(
        {"query": "test"}, cwd="/x", retriever=r
    )
    assert not result.is_error
    assert r.search_called
    assert "legacy hit" in result.display_text