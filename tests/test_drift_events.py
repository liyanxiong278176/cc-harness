"""E5 T1.3 — drift_detected 工厂测试 (8 个)。

Source of truth: docs/superpowers/plans/2026-07-21-e5-drift-detection.md lines 848-1059.
"""
from __future__ import annotations

from cc_harness.reflection.events import drift_detected, ReflectionEvent


# --- 1. severity 三档 ---


def test_drift_severity_pos_low_rate():
    """drift_rate=0.1 < 0.2 → pos。"""
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="Caroline",
        drift_rate=0.1,
        total_groups=2,
        inconsistent_groups=0,
        records=[],
        reason="low drift",
    )
    assert ev.severity == "pos"
    assert ev.event_type == "drift_detected"


def test_drift_severity_ambig_medium_rate():
    """drift_rate=0.3 ∈ [0.2, 0.5) → ambig。"""
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="Caroline",
        drift_rate=0.3,
        total_groups=2,
        inconsistent_groups=1,
        records=[],
        reason="medium drift",
    )
    assert ev.severity == "ambig"


def test_drift_severity_neg_high_rate():
    """drift_rate=0.8 > 0.5 → neg。"""
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="Caroline",
        drift_rate=0.8,
        total_groups=2,
        inconsistent_groups=2,
        records=[],
        reason="high drift",
    )
    assert ev.severity == "neg"


# --- 2. 边界值 ---


def test_drift_boundary_0_2():
    """drift_rate=0.2 属于 [0.2, 0.5) → ambig(下界 inclusive)。"""
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="X",
        drift_rate=0.2,
        total_groups=1,
        inconsistent_groups=0,
        records=[],
        reason="boundary low",
    )
    assert ev.severity == "ambig"


def test_drift_boundary_0_5():
    """drift_rate=0.5 属于 >= 0.5 → neg(上界 inclusive)。"""
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="X",
        drift_rate=0.5,
        total_groups=1,
        inconsistent_groups=0,
        records=[],
        reason="boundary high",
    )
    assert ev.severity == "neg"


# --- 3. evidence shape ---


def test_drift_evidence_shape():
    """evidence 必须含 6 字段:entity / drift_rate / total_groups / inconsistent_groups / records / reason。"""
    ev = drift_detected(
        session_id="s1",
        turn_idx=7,
        entity="Caroline",
        drift_rate=0.6,
        total_groups=3,
        inconsistent_groups=2,
        records=[{"memory_id": "m1", "predicted": "1990"}],
        reason="conflicting birth year",
    )
    assert ev.event_type == "drift_detected"
    assert ev.severity == "neg"
    assert ev.session_id == "s1"
    assert ev.turn_idx == 7
    assert isinstance(ev.created_at, float)
    assert ev.evidence["entity"] == "Caroline"
    assert ev.evidence["drift_rate"] == 0.6
    assert ev.evidence["total_groups"] == 3
    assert ev.evidence["inconsistent_groups"] == 2
    assert ev.evidence["records"] == [{"memory_id": "m1", "predicted": "1990"}]
    assert ev.evidence["reason"] == "conflicting birth year"
    # is ReflectionEvent
    assert isinstance(ev, ReflectionEvent)


# --- 4. F2 source 字段 (R2 part 1) ---


def test_drift_event_has_source_drift():
    """F2: drift_detected 工厂显式设置 source='drift',区别于其他 reflection。"""
    ev = drift_detected(
        session_id="s1", turn_idx=1, entity="Caroline",
        drift_rate=0.5, total_groups=1, inconsistent_groups=1,
        records=[], reason="x",
    )
    assert ev.source == "drift"


# --- 5. 截断 ---


def test_drift_records_truncated_at_10():
    """20 条 records → evidence.records 存 10。"""
    recs = [{"memory_id": f"m{i}"} for i in range(20)]
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="X",
        drift_rate=0.4,
        total_groups=2,
        inconsistent_groups=1,
        records=recs,
        reason="truncation test",
    )
    assert len(ev.evidence["records"]) == 10


def test_drift_reason_truncated_at_500():
    """1000 字 reason → evidence.reason 存 500。"""
    long_reason = "x" * 1000
    ev = drift_detected(
        session_id="s1",
        turn_idx=1,
        entity="X",
        drift_rate=0.4,
        total_groups=2,
        inconsistent_groups=1,
        records=[],
        reason=long_reason,
    )
    assert len(ev.evidence["reason"]) == 500
