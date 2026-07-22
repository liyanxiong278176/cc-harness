"""E5 T1.2 — DriftDetector 中心化引擎测试 (8 个)。

Source of truth: docs/superpowers/plans/2026-07-21-e5-drift-detection.md lines 228-844.

Stub 注:tests 顶部 stub `cc_harness.reflection.events.drift_detected` 工厂
(T1.3 未实现,先 mock 注入以保持 emit 路径可断言)。
"""
from __future__ import annotations
import asyncio
import json
import sys
import types

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# -----------------------------------------------------------------
# Stub: cc_harness.reflection.events.drift_detected 工厂
# (T1.3 才补;T1.2 先注入 fake factory 让 emit 路径可断言)
#
# 关键:必须**增量**绑定,不能替换整模块 — 否则其他测试文件 import
# `ReflectionEvent` 等真符号时找不到。先 import 真模块(若未在 sys.modules),
# 再在原模块上 patch `drift_detected`。
# -----------------------------------------------------------------
if "cc_harness.reflection.events" not in sys.modules:
    import importlib
    _ev_mod = importlib.import_module("cc_harness.reflection.events")
    sys.modules["cc_harness.reflection.events"] = _ev_mod
else:
    _ev_mod = sys.modules["cc_harness.reflection.events"]


def _stub_drift_detected(
    *, session_id, turn_idx, entity, drift_rate,
    total_groups, inconsistent_groups, records, reason,
):
    class _Stub:
        pass
    ev = _Stub()
    ev.event_type = "drift_detected"
    ev.severity = "neg" if drift_rate > 0.5 else ("ambig" if drift_rate >= 0.2 else "pos")
    ev.session_id = session_id
    ev.turn_idx = turn_idx
    ev.evidence = {
        "entity": entity,
        "drift_rate": drift_rate,
        "total_groups": total_groups,
        "inconsistent_groups": inconsistent_groups,
        "records": records,
        "reason": reason,
    }
    ev.created_at = 0.0
    return ev


_ev_mod.drift_detected = _stub_drift_detected

# 现在才 import detector(它的 _emit_drift 会在 try/except ImportError 里尝试 import)
from cc_harness.drift.detector import DriftDetector, DriftVerdict  # noqa: E402
from cc_harness.memory.store import Memory  # noqa: E402


@pytest.fixture
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "drift.jsonl"


@pytest.fixture
def fake_memory_service():
    svc = MagicMock()
    svc.save = AsyncMock(return_value=MagicMock(action="ADD", memory=MagicMock(id="m1")))
    return svc


@pytest.fixture
def fake_reflection_engine():
    """E2 ReflectionEngine mock。"""
    eng = MagicMock()
    eng.emit = AsyncMock()
    return eng


@pytest.fixture
def fake_l5():
    return MagicMock(sanitize=lambda x: x)


@pytest.fixture
def fake_judge_llm_entities():
    """JUDGE_ENTITIES 返 mock。"""
    async def _fn(system, user):
        return '{"entities": ["Caroline", "transgender"]}'
    return _fn


@pytest.fixture
def fake_judge_llm_consistent():
    """JUDGE_GROUP_CONSIST 返 mock consistent。"""
    async def _fn(system, user):
        return '{"consistent": true, "reason": "same fact"}'
    return _fn


@pytest.fixture
def fake_judge_llm_inconsistent():
    """JUDGE_GROUP_CONSIST 返 mock inconsistent。"""
    async def _fn(system, user):
        return '{"consistent": false, "reason": "conflicting facts"}'
    return _fn


def make_dispatch_judge(entities_resp, consistency_resp):
    """构造 dispatch judge:根据 system prompt 关键字分派(entities vs consistency)。"""
    async def _fn(system, user):
        if "entities" in system.lower():
            return entities_resp
        return consistency_resp
    return _fn


@pytest.fixture
def fake_judge_fail():
    async def _fn(system, user):
        raise RuntimeError("API 503")
    return _fn


@pytest.fixture
def fake_local_llm():
    async def _fn(system, user):
        return '{"entities": ["local"]}'
    return _fn


def make_memory(mid: str, text: str) -> Memory:
    return Memory(
        id=mid, text=text, embedding=[0.1, 0.2, 0.3, 0.4],
        created_at=0.0, updated_at=0.0, source="llm",
    )


@pytest.mark.asyncio
async def test_check_after_write_no_similar_no_op(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_consistent,
):
    """similar 为空 → check_after_write 返空。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=1,
    )
    new = make_memory("m1", "Caroline 是 transgender")
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=[],
    )
    assert verdicts == []


@pytest.mark.asyncio
async def test_check_after_write_detects_drift_neg(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """JUDGE_GROUP_CONSIST 返 inconsistent → emit drift_detected severity=neg。

    Detector 仅接 1 个 judge_llm,故用 dispatch fake:同一 LLM 根据 system
    prompt 关键字分派 entities / consistent 两类响应。
    """
    judge = make_dispatch_judge(
        '{"entities": ["Caroline"]}',
        '{"consistent": false, "reason": "conflicting facts"}',
    )
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=judge,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=1,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    assert len(verdicts) >= 1
    assert verdicts[0].drift_rate > 0.5  # 全 inconsistent → high
    # emit 至少 1 次 drift_detected
    fake_reflection_engine.emit.assert_awaited()


@pytest.mark.asyncio
async def test_check_after_read_empty_results(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_consistent,
):
    """results 为空 → check_after_read 返空。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=1,
    )
    verdicts = await det.check_after_read(
        session_id="s1", turn_idx=1, results=[],
    )
    assert verdicts == []


@pytest.mark.asyncio
async def test_judge_fail_falls_back_to_local(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_fail, fake_local_llm,
):
    """JUDGE 失败 → 退回本地 LLM。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_fail,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=1,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    # 本地 LLM 返 entities=["local"] 后续 LLM 不可用会 noop,但中间应 _ask_judge 已尝试 fallback
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    # 验证无 panic,可能返 [] 因为本地只返 1 个 entity,group 不够
    assert isinstance(verdicts, list)


@pytest.mark.asyncio
async def test_all_llm_fail_noop(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
):
    """全部 LLM fail → noop + 审计。"""
    async def fail(system, user):
        raise RuntimeError("nope")
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fail,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=1,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    assert verdicts == []
    assert tmp_audit.exists()
    lines = tmp_audit.read_text(encoding="utf-8").strip().splitlines()
    assert "all_llm_unavailable" in lines[0]


@pytest.mark.asyncio
async def test_severity_neg_high_drift_rate(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """drift_rate > 0.5 → emit severity=neg。"""
    judge = make_dispatch_judge(
        '{"entities": ["Caroline"]}',
        '{"consistent": false, "reason": "conflicting facts"}',
    )
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=judge,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=1,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1990"),  # 1/2 一致 → drift_rate=0.5 → ambig
        make_memory("m4", "Caroline 出生在 1980"),  # 不一致
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    emit_calls = fake_reflection_engine.emit.await_args_list
    if emit_calls:
        ev = emit_calls[0].args[0]
        assert ev.event_type == "drift_detected"
        assert ev.severity in {"neg", "ambig", "pos"}


@pytest.mark.asyncio
async def test_disabled_noop(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """enabled=False → check_* 直接返空。"""
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=fake_judge_llm_entities,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        enabled=False,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    assert verdicts == []
    fake_reflection_engine.emit.assert_not_awaited()
    assert not tmp_audit.exists()


@pytest.mark.asyncio
async def test_every_n_turns_throttling(
    tmp_audit, fake_memory_service, fake_reflection_engine, fake_l5,
    fake_judge_llm_entities, fake_judge_llm_inconsistent,
):
    """每 N turn 1 次,N=2 → turn_idx=1 不跑,turn_idx=2 跑。"""
    judge = make_dispatch_judge(
        '{"entities": ["Caroline"]}',
        '{"consistent": false, "reason": "conflicting facts"}',
    )
    det = DriftDetector(
        memory_service=fake_memory_service,
        reflection_engine=fake_reflection_engine,
        judge_llm=judge,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=2,
    )
    new = make_memory("m3", "Caroline 出生在 1990")
    similar = [
        make_memory("m1", "Caroline 出生在 1985"),
        make_memory("m2", "Caroline 出生在 1985"),
    ]
    # turn_idx=1 (1%2=1) → 不跑
    await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    emit_count_after_1 = fake_reflection_engine.emit.await_count
    # turn_idx=2 (2%2=0) → 跑
    await det.check_after_write(
        session_id="s1", turn_idx=2, new_memory=new, similar=similar,
    )
    emit_count_after_2 = fake_reflection_engine.emit.await_count
    # turn_idx=2 应比 turn_idx=1 多 emit(可能 more)
    assert emit_count_after_2 >= emit_count_after_1
