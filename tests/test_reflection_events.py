# tests/test_reflection_events.py
from cc_harness.reflection.events import (
    max_iter_reached, empty_turn_loop,
    tool_error_burst, tool_retry_burst, subagent_failed, decider_rollback,
)


def test_max_iter_reached_factory():
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="...")
    assert ev.event_type == "max_iter"
    assert ev.severity == "neg"
    assert ev.session_id == "s1"
    assert ev.turn_idx == 3
    assert ev.evidence["iter_used"] == 20
    assert isinstance(ev.created_at, float)


def test_empty_turn_loop_factory():
    ev = empty_turn_loop(session_id="s1", turn_idx=4, attempts=2)
    assert ev.event_type == "empty_turn"
    assert ev.severity == "neg"


def test_tool_error_burst_factory():
    ev = tool_error_burst(
        session_id="s1", turn_idx=5,
        errors=[{"tool": "run_command", "error": "exit 1"}],
    )
    assert ev.event_type == "tool_error_burst"
    assert ev.severity == "neg"
    assert len(ev.evidence["errors"]) == 1


def test_tool_retry_burst_factory():
    ev = tool_retry_burst(
        session_id="s1", turn_idx=6,
        calls=[{"tool": "fs__read", "args": {"path": "/x.py"}, "count": 3}],
    )
    assert ev.event_type == "tool_retry_burst"
    assert ev.severity == "ambig"


def test_subagent_failed_factory():
    ev = subagent_failed(
        session_id="s1", turn_idx=7,
        result={"status": "failed", "task_id": "t1", "final_text": "..."},
    )
    assert ev.event_type == "subagent_failed"
    assert ev.severity == "neg"


def test_subagent_blocked_maps_ambig():
    ev = subagent_failed(
        session_id="s1", turn_idx=8,
        result={"status": "blocked", "task_id": "t1"},
    )
    assert ev.severity == "ambig"


def test_decider_rollback_factory():
    ev = decider_rollback(
        session_id="s1", turn_idx=9,
        save_result={"action": "ROLLBACK", "error": "conflict:contradicts"},
    )
    assert ev.event_type == "decider_rollback"
    assert ev.severity == "neg"


# --- F2 source 字段 (R2 part 1) ---


def test_reflection_event_source_default_none():
    """F2: 其他 6 事件工厂不传 source,默认 None(engine 兜底用 'reflection')。"""
    from cc_harness.reflection.events import max_iter_reached
    ev = max_iter_reached(session_id="s1", turn_idx=1, iter_used=20, last_content="...")
    assert ev.source is None
