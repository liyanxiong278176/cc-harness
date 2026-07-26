"""WebSessionStore SQLite CRUD 单测(用 :memory:)。"""
from pathlib import Path

from cc_harness.memory.store import MemoryStore
from cc_harness.memory.checkpoint import WebSessionStore
from cc_harness.web.sessions import SessionMeta


async def test_upsert_and_list_active():
    store = MemoryStore(db_path=Path(":memory:"), embedding_dim=4)
    await store.init_schema()
    ws = WebSessionStore(store)
    meta = SessionMeta(
        session_id="abc123", cwd=Path("/tmp"), mode="coding",
        created_at=1000.0, last_active_at=1000.0,
    )
    await ws.upsert(meta)
    active = await ws.list_active()
    assert len(active) == 1
    assert active[0].session_id == "abc123"
    assert active[0].mode == "coding"


async def test_delete_cascades_to_session_message():
    store = MemoryStore(db_path=Path(":memory:"), embedding_dim=4)
    await store.init_schema()
    ws = WebSessionStore(store)
    meta = SessionMeta(
        session_id="abc123", cwd=Path("/tmp"), mode="coding",
        created_at=1000.0, last_active_at=1000.0,
    )
    await ws.upsert(meta)
    # 模拟:在 session_checkpoint 插一条(会触发 FK)
    from cc_harness.memory.checkpoint import CheckpointService
    cs = CheckpointService(store)
    await cs.save(
        session_id="abc123", project_root=Path("/tmp"), mode="coding",
        turn_counter=0, started_at="2026-07-25T00:00:00",
        ended_at="2026-07-25T00:00:01", cross_session_mode="last_only",
        messages=[{"role":"user","content":"hi"}],
    )
    # 现在删 web_session,FK 应 cascade
    await ws.delete("abc123")
    cur = await store._db.execute("SELECT COUNT(*) FROM session_checkpoint WHERE session_id=?", ("abc123",))
    row = await cur.fetchone()
    assert row[0] == 0