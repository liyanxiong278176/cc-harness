"""E4 Task 4: Consolidation cluster + merge 测试."""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from cc_harness.memory.store import MemoryStore
from cc_harness.memory.maintenance.consolidation import _greedy_cluster, consolidate


def test_greedy_cluster_forms_groups():
    a = MagicMock(id="a", embedding=[1.0] + [0.0] * 1023)
    b = MagicMock(id="b", embedding=[0.99] + [0.01] + [0.0] * 1022)
    c = MagicMock(id="c", embedding=[0.98] + [0.02] + [0.0] * 1022)
    d = MagicMock(id="d", embedding=[0.0] + [0.0] * 1022 + [1.0])
    clusters = _greedy_cluster([a, b, c, d], threshold=0.05)
    sizes = sorted([len(c) for c in clusters])
    assert sizes == [1, 3]


@pytest.mark.asyncio
async def test_consolidate_no_llm_keeps_oldest():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb_similar = [0.99] + [0.01] + [0.0] * 1022
        m1 = await store.add("old1", emb_similar, "s")
        m2 = await store.add("new2", emb_similar, "s")
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=lambda t: emb_similar)
        deleted = await consolidate(store, embedder, llm=None, similarity_threshold=0.05, max_cluster_size=5)
        assert deleted == 1
        assert await store.get(m1.id) is not None
        assert await store.get(m2.id) is None
        await store.close()


@pytest.mark.asyncio
async def test_consolidate_cluster_too_large_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [1.0] + [0.0] * 1023
        for i in range(7):
            await store.add(f"m{i}", emb, "s")
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=lambda t: emb)
        deleted = await consolidate(store, embedder, llm=None, similarity_threshold=0.05, max_cluster_size=5)
        assert deleted == 0
        await store.close()
