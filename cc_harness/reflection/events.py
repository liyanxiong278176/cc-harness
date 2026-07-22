"""Reflection event dataclass + 6 event factories.

Each factory returns a `ReflectionEvent` ready to be fed to
`ReflectionEngine.emit()`. The factory shape hides evidence shape details
so callers in `agent.py` / `subagent.py` only need to import the function
name.
"""
from __future__ import annotations
import time
from dataclasses import dataclass


@dataclass
class ReflectionEvent:
    event_type: str            # "max_iter" | "empty_turn" | "tool_error_burst" | "tool_retry_burst" | "subagent_failed" | "decider_rollback" | "drift_detected"
    severity: str              # "neg" | "ambig" | "pos"
    evidence: dict             # 原始事件载荷(去 PII,emit 前过 L5)
    session_id: str
    turn_idx: int
    created_at: float          # time.time() — 避免 datetime.now() 阻塞
    source: str | None = None  # F2: drift 用 'drift',其他默认 None → engine 兜底 'reflection'


def max_iter_reached(*, session_id: str, turn_idx: int, iter_used: int, last_content: str) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="max_iter",
        severity="neg",
        evidence={"iter_used": iter_used, "last_content": last_content[:500]},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def empty_turn_loop(*, session_id: str, turn_idx: int, attempts: int) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="empty_turn",
        severity="neg",
        evidence={"attempts": attempts},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def tool_error_burst(*, session_id: str, turn_idx: int, errors: list[dict]) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="tool_error_burst",
        severity="neg",
        evidence={"errors": errors[:10]},  # 截断 10 条
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def tool_retry_burst(*, session_id: str, turn_idx: int, calls: list[dict]) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="tool_retry_burst",
        severity="ambig",
        evidence={"calls": calls[:10]},
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def subagent_failed(*, session_id: str, turn_idx: int, result: dict) -> ReflectionEvent:
    status = result.get("status", "unknown")
    severity = "neg" if status in {"failed", "incomplete", "timeout"} else (
        "ambig" if status == "blocked" else "pos"
    )
    return ReflectionEvent(
        event_type="subagent_failed",
        severity=severity,
        evidence={
            "status": status,
            "task_id": result.get("task_id"),
            "final_text": (result.get("final_text") or "")[:500],
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


def decider_rollback(*, session_id: str, turn_idx: int, save_result: dict) -> ReflectionEvent:
    return ReflectionEvent(
        event_type="decider_rollback",
        severity="neg",
        evidence={
            "action": save_result.get("action"),
            "error": save_result.get("error"),
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )


# E5 漂移检测
def drift_detected(
    *,
    session_id: str,
    turn_idx: int,
    entity: str,
    drift_rate: float,
    total_groups: int,
    inconsistent_groups: int,
    records: list[dict],
    reason: str,
) -> ReflectionEvent:
    """E5 drift 事件:同 entity 多 record predicted 不一致。

    severity 按 drift_rate 三档:
      < 0.2  → pos(健康,长期观测)
      0.2-0.5 → ambig(轻度,可能 E4 consolidation 后续合并)
      > 0.5  → neg(严重,需立即关注)
    """
    if drift_rate < 0.2:
        severity = "pos"
    elif drift_rate < 0.5:
        severity = "ambig"
    else:
        severity = "neg"
    return ReflectionEvent(
        event_type="drift_detected",
        severity=severity,
        evidence={
            "entity": entity,
            "drift_rate": drift_rate,
            "total_groups": total_groups,
            "inconsistent_groups": inconsistent_groups,
            "records": records[:10],  # 截断 10 条
            "reason": reason[:500],  # 截断 500 字
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
        source="drift",  # F2
    )
