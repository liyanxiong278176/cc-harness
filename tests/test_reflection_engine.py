"""Tests for cc_harness.reflection.engine.ReflectionEngine (E2 T1.3).

7 cases cover: emit immediacy, judge success → save, judge fail → local fallback,
all-llm fail → noop + audit, get_last_neg_reflection, disabled noop, lock dedup.
All cases use MagicMock / AsyncMock — production path contains 0 MagicMock.
"""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from cc_harness.reflection.engine import ReflectionEngine
from cc_harness.reflection.events import max_iter_reached


@pytest.fixture
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "reflection.jsonl"


@pytest.fixture
def fake_memory_service():
    svc = MagicMock()
    svc.save = AsyncMock(return_value=MagicMock(action="ADD", memory=MagicMock(id="m1")))
    return svc


@pytest.fixture
def fake_l5():
    return MagicMock(sanitize=lambda x: x)


@pytest.fixture
def fake_judge_llm():
    """模拟 JUDGE_MODEL 返 JSON。"""
    async def _fn(system, user):
        return '{"reflection": "失败根因: 没用 Grep 先查。", "tags": ["grep"]}'
    return _fn


@pytest.fixture
def fake_judge_fail():
    """模拟 JUDGE_MODEL 抛错。"""
    async def _fn(system, user):
        raise RuntimeError("API 503")
    return _fn


@pytest.fixture
def fake_local_llm():
    async def _fn(system, user):
        return '{"reflection": "本地兜底反思。", "tags": ["fallback"]}'
    return _fn


@pytest.mark.asyncio
async def test_emit_returns_immediately(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    """emit() 立即返回,不阻塞 turn。"""
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    t0 = asyncio.get_event_loop().time()
    await eng.emit(ev)
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 0.05  # emit 必须 < 50ms


@pytest.mark.asyncio
async def test_judge_success_writes_to_memory(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_awaited_once()
    call = fake_memory_service.save.await_args
    assert call.kwargs["source"] == "reflection" or call.args[1] == "reflection"


@pytest.mark.asyncio
async def test_judge_fail_falls_back_to_local(tmp_audit, fake_memory_service, fake_l5, fake_judge_fail, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_fail, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_awaited_once()  # 本地兜底成功 → 仍写


@pytest.mark.asyncio
async def test_all_llm_fail_noop(tmp_audit, fake_memory_service, fake_l5):
    async def fail(system, user):
        raise RuntimeError("nope")
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fail,
        judge_llm=fail, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_not_awaited()
    assert tmp_audit.exists()
    lines = tmp_audit.read_text(encoding="utf-8").strip().splitlines()
    assert "all_llm_unavailable" in lines[0]


@pytest.mark.asyncio
async def test_get_last_neg_reflection_updates(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    assert eng.get_last_neg_reflection() is None
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    last = eng.get_last_neg_reflection()
    assert last is not None
    assert "Grep" in last or "失败" in last


@pytest.mark.asyncio
async def test_disabled_noop(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
        enabled=False,
    )
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    await eng.emit(ev)
    await eng._drain(timeout_s=2)
    fake_memory_service.save.assert_not_awaited()
    assert not tmp_audit.exists()


@pytest.mark.asyncio
async def test_lock_prevents_duplicate(tmp_audit, fake_memory_service, fake_l5, fake_judge_llm, fake_local_llm):
    """同 event_type+session+turn_idx 5s 内只跑一次。"""
    eng = ReflectionEngine(
        memory_service=fake_memory_service, llm_client=fake_local_llm,
        judge_llm=fake_judge_llm, l5_engine=fake_l5,
        project_root=tmp_audit.parent, audit_path=tmp_audit,
    )
    ev1 = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    ev2 = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="y")
    await eng.emit(ev1)
    await eng.emit(ev2)  # 5s 内重复
    await eng._drain(timeout_s=2)
    assert fake_memory_service.save.await_count == 1
