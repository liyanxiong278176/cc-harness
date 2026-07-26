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
