"""E3 T2: CheckpointService round-trip + project_root 过滤 + list_recent 测试。"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from cc_harness.memory.store import MemoryStore
from cc_harness.memory.checkpoint import CheckpointService


@pytest.mark.asyncio
async def test_checkpoint_save_load_messages_roundtrip():
    """E3 D1: save 5 messages + load → 全字段等值(含 tool_calls / multimodal)。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        svc = CheckpointService(store)
        messages = [
            {"role": "system", "content": "you are cc-harness"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "[]"},
            {"role": "user", "content": [{"type": "text", "text": "img"}, {"type": "image_url"}]},
        ]
        await svc.save(
            session_id="s1", project_root=pathlib.Path("/tmp/projA"),
            mode="coding", turn_counter=3,
            started_at="2026-07-24T10:00:00",
            ended_at="2026-07-24T10:05:00",
            cross_session_mode="last_only",
            messages=messages,
        )
        loaded = await svc.load_messages("s1")
        assert loaded == messages
        await store.close()


@pytest.mark.asyncio
async def test_load_latest_filters_by_project_root():
    """E3 D2: load_latest 按 project_root 过滤,不同 project → None。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        svc = CheckpointService(store)
        await svc.save(session_id="s1", project_root=pathlib.Path("/projA"),
                       mode="coding", turn_counter=1,
                       started_at="2026-07-24T09:00:00",
                       ended_at="2026-07-24T09:05:00",
                       cross_session_mode="last_only", messages=[])
        await svc.save(session_id="s2", project_root=pathlib.Path("/projA"),
                       mode="coding", turn_counter=2,
                       started_at="2026-07-24T10:00:00",
                       ended_at="2026-07-24T10:05:00",
                       cross_session_mode="last_only", messages=[])
        # 不同 project → None
        assert await svc.load_latest(pathlib.Path("/projB")) is None
        # 同 project → 最新 (s2)
        latest = await svc.load_latest(pathlib.Path("/projA"))
        assert latest is not None
        assert latest.session_id == "s2"
        assert latest.turn_counter == 2
        await store.close()


@pytest.mark.asyncio
async def test_load_latest_returns_none_when_empty():
    """E3 D2: 无 checkpoint 时 load_latest 返 None。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        svc = CheckpointService(store)
        assert await svc.load_latest(pathlib.Path("/any")) is None
        await store.close()


@pytest.mark.asyncio
async def test_list_recent_returns_by_ended_at_desc():
    """E3 D2: list_recent 按 ended_at DESC 返回。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        svc = CheckpointService(store)
        for i, ts in enumerate(["2026-07-24T09:00:00", "2026-07-24T10:00:00", "2026-07-24T11:00:00"]):
            await svc.save(
                session_id=f"s{i}", project_root=pathlib.Path("/p"),
                mode="coding", turn_counter=i,
                started_at=ts, ended_at=ts,
                cross_session_mode="last_only", messages=[],
            )
        recent = await svc.list_recent(pathlib.Path("/p"), limit=2)
        assert [r.session_id for r in recent] == ["s2", "s1"]
        await store.close()
