import pytest
import tempfile
from pathlib import Path
from cc_harness.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_migrate_adds_e4_columns():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        cur = await store._db.execute("PRAGMA table_info(memories)")
        cols = {r[1] for r in await cur.fetchall()}
        for col in ("staleness", "recall_count", "last_recalled_at",
                    "cluster_id", "merged_from"):
            assert col in cols, f"missing column: {col}"
        await store.close()


@pytest.mark.asyncio
async def test_migrate_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = MemoryStore(path, embedding_dim=1024)
        await store.init_schema()
        await store._migrate()
        await store.close()
