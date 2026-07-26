"""SessionManager 单测:create / list / delete / push_event / max 上限。"""
import asyncio

import pytest

from cc_harness.web.sessions import SessionManager


class FakeLLM:
    async def chat(self, *args, **kwargs):
        raise NotImplementedError


class FakeMCPFactory:
    async def __call__(self):
        return None  # 不启 MCP


@pytest.fixture
def manager(tmp_path):
    return SessionManager(
        llm=FakeLLM(),
        mcp_factory=FakeMCPFactory(),
        web_session_store=None,  # 内存模式,无持久化
        max_sessions=2,
    )


async def test_create_returns_record_with_unique_id(manager, tmp_path):
    rec = await manager.create(cwd=tmp_path, mode="coding")
    assert rec.meta.session_id
    assert rec.meta.cwd == tmp_path
    assert rec.meta.mode == "coding"
    assert rec.meta.status == "active"


async def test_list_returns_all_sessions(manager, tmp_path):
    a = await manager.create(cwd=tmp_path, mode="coding")
    b = await manager.create(cwd=tmp_path, mode="plan")
    metas = await manager.list()
    ids = {m.session_id for m in metas}
    assert a.meta.session_id in ids
    assert b.meta.session_id in ids


async def test_delete_removes_session(manager, tmp_path):
    rec = await manager.create(cwd=tmp_path, mode="coding")
    await manager.delete(rec.meta.session_id)
    assert await manager.get(rec.meta.session_id) is None


async def test_max_sessions_enforced(manager, tmp_path):
    await manager.create(cwd=tmp_path, mode="coding")
    await manager.create(cwd=tmp_path, mode="plan")
    with pytest.raises(ValueError, match="max_sessions"):
        await manager.create(cwd=tmp_path, mode="design")


async def test_push_event_lands_in_queue(manager, tmp_path):
    from cc_harness.web.events import ThoughtEvent
    rec = await manager.create(cwd=tmp_path, mode="coding")
    await manager.push_event(rec.meta.session_id, ThoughtEvent(text="hi", iteration=0))
    # 给 queue.get() 一点时间
    ev = await asyncio.wait_for(rec.event_queue.get(), timeout=1.0)
    assert ev.text == "hi"


async def test_emitter_pushes_thought_through_l5(manager, tmp_path):
    """Emitter 把 thought dict 转 Event,push 到 queue。"""
    from cc_harness.web.emitter import EventEmitter
    from cc_harness.web.events import ThoughtEvent
    rec = await manager.create(cwd=tmp_path, mode="coding")
    emitter = EventEmitter(manager, rec.meta.session_id, l5_engine=None)
    await emitter({"type": "thought", "text": "openai_key = sk-abc", "iteration": 0, "ts": 0.0})
    ev = await asyncio.wait_for(rec.event_queue.get(), timeout=1.0)
    assert isinstance(ev, ThoughtEvent)
    assert "sk-abc" in ev.text or "[REDACTED" in ev.text  # L5 没装 → 原文


async def test_emitter_observation_not_scanned(manager, tmp_path):
    """observation 不过 L5(沿 CLAUDE.md M3:工具观察不扫)。"""
    from cc_harness.web.emitter import EventEmitter
    rec = await manager.create(cwd=tmp_path, mode="coding")
    emitter = EventEmitter(manager, rec.meta.session_id, l5_engine=None)
    await emitter({
        "type": "observation", "text": "包含 key=sk-xyz",
        "is_error": False, "duration_ms": 1, "iteration": 0, "ts": 0.0,
    })
    ev = await asyncio.wait_for(rec.event_queue.get(), timeout=1.0)
    assert ev.text == "包含 key=sk-xyz"  # 不脱敏


async def test_restore_from_checkpoint_no_store(manager):
    """web_session_store is None → restore_from_checkpoint 直接 return, 不抛。"""
    await manager.restore_from_checkpoint()  # 不抛即通过
    assert await manager.list() == []


async def test_restore_from_checkpoint_with_store(tmp_path):
    """web_session_store 有 active sessions → 还原进内存,state=None 占位。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.checkpoint import WebSessionStore, SessionMeta

    mem_store = MemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    await mem_store.init_schema()
    ws = WebSessionStore(mem_store)
    for sid, mode in [("aaa", "coding"), ("bbb", "plan")]:
        await ws.upsert(SessionMeta(
            session_id=sid, cwd=tmp_path, mode=mode,
            created_at=1000.0, last_active_at=1000.0,
        ))

    mgr = SessionManager(
        llm=FakeLLM(), mcp_factory=FakeMCPFactory(),
        web_session_store=ws, max_sessions=8,
    )
    await mgr.restore_from_checkpoint()
    sessions = await mgr.list()
    assert {s.session_id for s in sessions} == {"aaa", "bbb"}
    # state 一定是 None(Task 11+ 才接 ReplState);task 一定是 None(不 spawn)
    rec_a = await mgr.get("aaa")
    rec_b = await mgr.get("bbb")
    assert rec_a is not None and rec_b is not None
    assert rec_a.state is None and rec_b.state is None
    assert rec_a.task is None and rec_b.task is None


async def test_restore_from_checkpoint_empty_store(manager, tmp_path):
    """store 存在但无 active sessions → restore 不抛,sessions 空。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.checkpoint import WebSessionStore

    mem_store = MemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    await mem_store.init_schema()
    mgr = SessionManager(
        llm=FakeLLM(), mcp_factory=FakeMCPFactory(),
        web_session_store=WebSessionStore(mem_store), max_sessions=8,
    )
    await mgr.restore_from_checkpoint()
    assert await mgr.list() == []
