"""状态守卫测试(spec 组件 3, 19 case parametrize + 1 终态消息断言)。

策略:按 spec 完整规则表枚举合法转移(13 个)+ 非法转移(5 个)。
13 个合法中包含 2 个 idempotent(pending→pending, in_progress→in_progress),
与 plan lines 357-359 对齐(idempotent 同状态视为合法)。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from cc_harness.project.models import TodoTask
from cc_harness.project.status import StatusGuardError, status_guard


def _task(status: str = "pending") -> TodoTask:
    now = datetime.now()
    return TodoTask(
        id="abc12345",
        title="t",
        status=status,
        created_at=now,
        updated_at=now,
        description="",
        depends_on=[],
        parent_task=None,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        active_sessions=[],
    )


# 合法转移 13 个(spec 组件 3 完整规则表 + plan idempotent 注释)
VALID_TRANSITIONS = [
    # pending
    ("pending", "in_progress"),
    ("pending", "cancelled"),
    ("pending", "blocked"),
    ("pending", "pending"),  # idempotent
    # in_progress
    ("in_progress", "pending"),
    ("in_progress", "done"),
    ("in_progress", "blocked"),
    ("in_progress", "cancelled"),
    ("in_progress", "in_progress"),  # idempotent
    # blocked
    ("blocked", "in_progress"),
    ("blocked", "cancelled"),
    ("blocked", "pending"),
    # cancelled
    ("cancelled", "pending"),
]


@pytest.mark.parametrize("current,target", VALID_TRANSITIONS)
def test_status_guard_valid_transitions(current: str, target: str) -> None:
    """合法转移不抛异常。"""
    status_guard(_task(current), target)


# 非法转移 5 个(done 终态 4 种 + unknown target 1 种)
INVALID_TRANSITIONS = [
    ("done", "pending"),
    ("done", "in_progress"),
    ("done", "blocked"),
    ("done", "cancelled"),
    ("pending", "garbage"),
]


@pytest.mark.parametrize("current,target", INVALID_TRANSITIONS)
def test_status_guard_invalid_transitions(current: str, target: str) -> None:
    """非法转移必须抛 StatusGuardError。"""
    with pytest.raises(StatusGuardError):
        status_guard(_task(current), target)


def test_status_guard_done_terminal_message() -> None:
    """done 终态错误消息必须含 'done is terminal' 字样(plan lines 376-378)。"""
    with pytest.raises(StatusGuardError, match="done is terminal"):
        status_guard(_task("done"), "pending")


def test_status_guard_error_includes_task_id_and_states() -> None:
    """非法转移消息应包含 task id、当前状态、目标状态,便于 debug。"""
    task = _task("in_progress")
    with pytest.raises(StatusGuardError) as exc_info:
        status_guard(task, "garbage")
    msg = str(exc_info.value)
    assert "abc12345" in msg
    assert "in_progress" in msg
    assert "garbage" in msg


def test_status_guard_error_message_non_done_includes_illegal_phrase() -> None:
    """非 done 状态的非法转移消息应说明是 illegal transition(区别于 done terminal)。"""
    with pytest.raises(StatusGuardError, match="illegal status transition"):
        status_guard(_task("pending"), "garbage")


def test_status_guard_unknown_current_status_raises() -> None:
    """Task 2 review followup:当前 status 不在表里(损坏 yaml)→ StatusGuardError 而非 KeyError。"""
    task = _task("pending")
    # 直接构造非法当前 status(模拟外部编辑损坏 yaml)
    object.__setattr__(task, "status", "corrupted_state")
    with pytest.raises(StatusGuardError, match="unknown current status"):
        status_guard(task, "done")


def test_status_guard_same_state_transitions_accepted_for_all_states() -> None:
    """Task 2 review followup:4 个非终态的同状态转移(idempotent)全部合法。

    plan lines 357-359 只列了 pending→pending / in_progress→in_progress。
    实现接受全部 4 个非终态;done 是终态,done→done 抛(见 test_status_guard_done_terminal_message)。
    """
    for s in ("pending", "in_progress", "blocked", "cancelled"):
        status_guard(_task(s), s)  # 不抛


def test_status_guard_inherits_todo_error() -> None:
    """Task 3 起 StatusGuardError 必须继承 TodoError,纳入统一异常层级。"""
    from cc_harness.project.exceptions import TodoError
    assert issubclass(StatusGuardError, TodoError)