"""E3 T1: session_checkpoint / session_message schema 验证。"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from cc_harness.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_session_checkpoint_table_exists():
    """E3 D2: session_checkpoint 表存在且含 8 列。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        cols = {r[1] for r in (await (await store._db.execute(
            "SELECT * FROM pragma_table_info('session_checkpoint')"
        )).fetchall())}
        expected = {"session_id", "project_root", "mode", "turn_counter",
                    "started_at", "ended_at", "cross_session_mode", "extra_json"}
        assert expected.issubset(cols), f"missing: {expected - cols}"
        await store.close()


@pytest.mark.asyncio
async def test_session_message_table_and_index_exist():
    """E3 D2: session_message 表 + 6 列 + idx 存在。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        cols = {r[1] for r in (await (await store._db.execute(
            "SELECT * FROM pragma_table_info('session_message')"
        )).fetchall())}
        assert {"id", "session_id", "turn_idx", "role", "content_json", "ts"}.issubset(cols)
        idx = {r[0] for r in (await (await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_session_message_session_turn'"
        )).fetchall())}
        assert "idx_session_message_session_turn" in idx
        await store.close()


@pytest.mark.asyncio
async def test_session_message_cascade_delete():
    """E3 D2: FK ON DELETE CASCADE — 删 checkpoint 自动删 messages。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        await store._db.execute(
            "INSERT OR REPLACE INTO session_checkpoint "
            "(session_id, project_root, mode, turn_counter, started_at, ended_at, cross_session_mode, extra_json) "
            "VALUES ('s1', '/tmp', 'coding', 5, '2026-07-24T10:00:00', '2026-07-24T10:05:00', 'last_only', '{}')"
        )
        await store._db.execute(
            "INSERT INTO session_message (session_id, turn_idx, role, content_json, ts) "
            "VALUES ('s1', 0, 'user', '{}', '2026-07-24T10:00:00')"
        )
        await store._db.commit()
        # FK CASCADE 需要 PRAGMA foreign_keys=ON
        await store._db.execute("PRAGMA foreign_keys = ON")
        await store._db.execute("DELETE FROM session_checkpoint WHERE session_id='s1'")
        await store._db.commit()
        cnt = (await (await store._db.execute(
            "SELECT COUNT(*) FROM session_message WHERE session_id='s1'"
        )).fetchone())[0]
        assert cnt == 0, f"expected 0 messages after cascade, got {cnt}"
        await store.close()