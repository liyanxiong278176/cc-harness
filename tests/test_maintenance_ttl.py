"""E4 Task 3: TTL 过期清理算子测试."""
import pytest
import tempfile
from pathlib import Path
from cc_harness.memory.store import MemoryStore
from cc_harness.memory.maintenance.ttl import purge_stale


@pytest.mark.asyncio
async def test_purge_below_threshold_keeps():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [0.0] * 1024
        m = await store.add("text", emb, "s")
        await store.update_staleness_bulk({m.id: 0.5})
        deleted = await purge_stale(store, staleness_threshold=0.85, limit=100)
        assert deleted == []
        assert await store.get(m.id) is not None
        await store.close()


@pytest.mark.asyncio
async def test_purge_above_threshold_removes():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [0.0] * 1024
        m1 = await store.add("stale", emb, "s")
        m2 = await store.add("fresh", emb, "s")
        await store.update_staleness_bulk({m1.id: 0.9, m2.id: 0.3})
        deleted = await purge_stale(store, staleness_threshold=0.85, limit=100)
        assert m1.id in deleted
        assert m2.id not in deleted
        assert await store.get(m1.id) is None
        assert await store.get(m2.id) is not None
        await store.close()


@pytest.mark.asyncio
async def test_purge_respects_limit():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        emb = [0.0] * 1024
        ids = []
        for i in range(5):
            m = await store.add(f"stale{i}", emb, "s")
            ids.append(m.id)
        await store.update_staleness_bulk({i: 0.9 for i in ids})
        deleted = await purge_stale(store, staleness_threshold=0.85, limit=3)
        assert len(deleted) == 3
        await store.close()
