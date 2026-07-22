"""E5 round 2 — DriftDetector 修复测试。

Round 1 已被 final review 抓到 6 bug,本文件对应修法:
- F4: 移除 _memory_service 形参(不再 declare 没用的形参)
- F5: consistency judge 失败时返 (None, reason),_check_groups 不发 drift event
- F6: 多组 ratio 算法,drift_rate = inconsistent_groups / total_groups
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from cc_harness.drift.detector import DriftDetector, DriftVerdict  # noqa: F401
from cc_harness.memory.store import Memory


@pytest.fixture
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "drift.jsonl"


@pytest.fixture
def fake_reflection_engine():
    eng = MagicMock()
    eng.emit = AsyncMock()
    return eng


@pytest.fixture
def fake_l5():
    return MagicMock(sanitize=lambda x: x)


def make_memory(mid: str, text: str, source: str = "llm") -> Memory:
    return Memory(
        id=mid, text=text, embedding=[0.1, 0.2, 0.3, 0.4],
        created_at=0.0, updated_at=0.0, source=source,
    )


@pytest.mark.asyncio
async def test_check_after_write_with_two_similar_inconsistent(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F6: 2 同 entity 不同 text → 2 组 → 2 inconsistent → drift_rate = 2/2 = 1.0 → severity=neg。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(chat=AsyncMock()),  # 不走 chat 路径,直接用 _judge_entities 的可调函数
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    # 用 monkeypatch 替换 _judge_entities / _judge_group_consistency 的内部调用
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different years"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    assert len(verdicts) == 1
    assert verdicts[0].total_groups == 2
    assert verdicts[0].inconsistent_groups == 2
    assert verdicts[0].drift_rate == 1.0  # F6: ratio 算法
    # emit 1 次 drift_detected severity=neg
    fake_reflection_engine.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_after_write_multigroup_ratio(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F6: 4 records 同 entity "caroline",3 个不同 group(2 重复算 1 组),1 inconsistent,1 consistent → drift_rate = 1/3 ≈ 0.333 → ambig。

    m5 风格多组:mems 按 text.strip().lower() 分组,每组跑 consistency judge。
    """
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    # 4 records: 2 个 "caroline 1985" 同组 + "caroline 1990" + "caroline 1980"
    # groups = {"caroline 1985": [m1, m2], "caroline 1990": [m3, new], "caroline 1980": [m4]} = 3 组
    # 一致性判断:3 组中 1 组 consistent,2 组 inconsistent → drift_rate = 2/3 ≈ 0.667 → neg
    judge_calls = []

    async def judge_consist(entity, records):
        # 第一组("caroline 1985" 双份)→ consistent=True
        # 第二组("caroline 1990")→ consistent=False
        # 第三组("caroline 1980")→ consistent=False
        text_set = {m.text.strip().lower() for m in records}
        if "caroline 1990" in text_set or "caroline 1980" in text_set:
            judge_calls.append("inconsistent")
            return False, "different"
        judge_calls.append("consistent")
        return True, "same"

    det._judge_group_consistency = AsyncMock(side_effect=judge_consist)

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
        make_memory("m4", "Caroline 1980"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    assert len(verdicts) == 1
    assert verdicts[0].total_groups == 3
    assert verdicts[0].inconsistent_groups == 2
    assert abs(verdicts[0].drift_rate - 2/3) < 0.01  # F6: ~0.667
    # 注:neg severity 因为 > 0.5
    fake_reflection_engine.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_consistency_judge_fail_returns_none(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F5: consistency judge 失败(parse_error / all_llm_unavailable)→ 返 (None, reason),_check_groups 不发 drift event。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(None, "parse_error"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
    ]
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    # verdict 不返回(consistent=None 不算 inconsistent)
    assert verdicts == []
    # emit 不调
    fake_reflection_engine.emit.assert_not_awaited()
    # 审计:consistency_judge_failed (区别于 all_llm_unavailable)
    assert tmp_audit.exists()
    line = tmp_audit.read_text(encoding="utf-8").strip()
    assert "consistency_judge_failed" in line


@pytest.mark.asyncio
async def test_judge_failure_falls_back_to_local_llm(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """F3: judge_llm 抛 → _local_llm 接管 → 正常返回。"""
    primary_called = []
    local_called = []

    async def primary_fn(system, user):
        primary_called.append("x")
        raise RuntimeError("primary down")

    async def local_fn(system, user):
        local_called.append("x")
        return '{"entities": ["caroline"]}'

    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=primary_fn,
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        local_llm=local_fn,
    )
    # 触发 _judge_entities(调用 _ask_judge)
    result = await det._judge_entities("Caroline 1990")
    assert result == ["caroline"]
    assert len(primary_called) == 1
    assert len(local_called) == 1


@pytest.mark.asyncio
async def test_drift_audit_records_entity_hash(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """Round 1 已验:audit 写 entity_hash 不写 entity 明文,本测试保证 F6 后仍 work。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["Caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1990"),
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    audit_text = tmp_audit.read_text(encoding="utf-8")
    # 明文不应出现
    assert "Caroline" not in audit_text  # 唯一 Caroline 是 entity 名,audit 不该有
    # 哈希字段存在
    assert "entity_hash" in audit_text


# --- M3: sample_records text 走 L5 sanitize ---


@pytest.mark.asyncio
async def test_sample_records_passes_l5_sanitize(
    tmp_audit, fake_reflection_engine,
):
    """M3: detector 给 drift_detected 工厂的 sample_records text 已被 l5.sanitize 替换。"""
    fake_l5 = MagicMock()
    # L5 替换 text 包含 '[REDACTED:phone]'
    fake_l5.sanitize = MagicMock(side_effect=lambda x: x.replace("555-1234", "[REDACTED:phone]"))

    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline phone 555-1234"),  # 包含 PII
        make_memory("m2", "Caroline 1985"),
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    # fake_reflection_engine.emit 收到的事件,records[0]['text'] 应被 sanitize
    emit_event = fake_reflection_engine.emit.await_args.args[0]
    # evidence 里的 records
    rec_texts = [r["text"] for r in emit_event.evidence["records"]]
    # 含 PII 的那条应被 [REDACTED:phone] 替换
    assert any("[REDACTED:phone]" in t for t in rec_texts)
    assert not any("555-1234" in t for t in rec_texts)


# --- M4: 断言加强 ---


@pytest.mark.asyncio
async def test_severity_neg_high_drift_rate(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """M4: drift_rate=0.5 → _severity_for 落 'else: neg'(0.5 < 0.5 False),severity 应 == "neg"。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
    )
    # 构造 2 组:1 inconsistent + 1 consistent → drift_rate = 1/2 = 0.5
    det._judge_entities = AsyncMock(return_value=["caroline"])
    judge_calls = []

    async def judge_consist(entity, records):
        text_set = {m.text.strip().lower() for m in records}
        if "caroline 1990" in text_set:
            judge_calls.append("inconsistent")
            return False, "different"
        judge_calls.append("consistent")
        return True, "same"

    det._judge_group_consistency = AsyncMock(side_effect=judge_consist)

    new = make_memory("m3", "Caroline 1990")  # 单独成组,inconsistent
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),  # 双份同组,consistent
    ]
    await det.check_after_write(
        session_id="s1", turn_idx=5, new_memory=new, similar=similar,
    )
    emit_calls = fake_reflection_engine.emit.await_args_list
    assert emit_calls, "expected at least one emit"
    ev = emit_calls[0].args[0]
    assert ev.event_type == "drift_detected"
    # drift_rate=0.5 → 0.5 < 0.5 False → else: "neg"
    assert ev.severity == "neg"


@pytest.mark.asyncio
async def test_every_n_turns_throttling(
    tmp_audit, fake_reflection_engine, fake_l5,
):
    """M4: every_n_turns=2 → turn_idx=1 (1%2=1) 不跑 → emit_count==0;
    turn_idx=2 (2%2=0) 跑 → emit_count==1(精确)。"""
    det = DriftDetector(
        reflection_engine=fake_reflection_engine,
        judge_llm=MagicMock(),
        l5_engine=fake_l5,
        project_root=tmp_audit.parent,
        audit_path=tmp_audit,
        every_n_turns=2,
    )
    det._judge_entities = AsyncMock(return_value=["caroline"])
    det._judge_group_consistency = AsyncMock(return_value=(False, "different"))

    new = make_memory("m3", "Caroline 1990")
    similar = [
        make_memory("m1", "Caroline 1985"),
        make_memory("m2", "Caroline 1985"),
    ]
    # turn_idx=1 (1%2=1) → _should_run False → 不 emit
    await det.check_after_write(
        session_id="s1", turn_idx=1, new_memory=new, similar=similar,
    )
    emit_count_after_1 = fake_reflection_engine.emit.await_count
    # turn_idx=2 (2%2=0) → _should_run True → emit 1 次
    await det.check_after_write(
        session_id="s1", turn_idx=2, new_memory=new, similar=similar,
    )
    emit_count_after_2 = fake_reflection_engine.emit.await_count
    # 精确断言
    assert emit_count_after_1 == 0, f"turn_idx=1 should not emit, got {emit_count_after_1}"
    assert emit_count_after_2 == 1, f"turn_idx=2 should emit exactly 1, got {emit_count_after_2}"
