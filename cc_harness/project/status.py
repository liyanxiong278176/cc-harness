"""Sub-project A 状态守卫(spec 组件 3)。

done 是不可逆终态;其他状态可双向流动。规则表来自 spec lines 297-301:

    pending      → in_progress / cancelled / blocked / pending(idempotent)
    in_progress  → pending / done / blocked / cancelled / in_progress(idempotent)
    blocked      → in_progress / cancelled / pending / blocked(idempotent,允许)
    cancelled    → pending / cancelled(idempotent,允许)
    done         → —(终态,任何转移抛 StatusGuardError)

合法转移(包括 idempotent 同状态)→ no-op。
非法转移 → raise StatusGuardError;消息区分 done 终态与其他非法转移。
"""
from __future__ import annotations

from cc_harness.project.exceptions import StatusGuardError as _StatusGuardError
from cc_harness.project.models import TaskStatus, TodoTask

# 状态转移表:frozenset 便于 O(1) 成员检查 + 不可变防意外修改
# TaskStatus 强类型 + new_status: str 接受不可信输入
_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    "pending": frozenset({"pending", "in_progress", "blocked", "cancelled"}),
    "in_progress": frozenset({"pending", "in_progress", "done", "blocked", "cancelled"}),
    "blocked": frozenset({"pending", "in_progress", "blocked", "cancelled"}),
    "cancelled": frozenset({"pending", "cancelled"}),
    "done": frozenset(),  # 终态:无任何允许目标
}


def status_guard(current: TodoTask, new_status: str) -> None:
    """校验状态转移合法性。非法则 raise StatusGuardError。

    Args:
        current: 当前 task(读其 status 与 id 用于错误消息)
        new_status: 目标状态字符串(可来自不可信输入)

    Raises:
        StatusGuardError: 转移非法(done 终态、当前状态未知、
            或 new_status 不在允许集合内)
    """
    # Defensive(Task 2 review):当前 status 不在表里 → StatusGuardError 而非 KeyError
    if current.status not in _ALLOWED_TRANSITIONS:
        raise StatusGuardError(
            f"unknown current status: task {current.id} has status "
            f"{current.status!r}, expected one of {sorted(_ALLOWED_TRANSITIONS.keys())}"
        )
    allowed = _ALLOWED_TRANSITIONS[current.status]
    if new_status in allowed:
        return
    if current.status == "done":
        raise StatusGuardError(
            f"done is terminal: task {current.id} cannot transition "
            f"from 'done' to '{new_status}'"
        )
    raise StatusGuardError(
        f"illegal status transition: task {current.id} "
        f"cannot go from '{current.status}' to '{new_status}'"
    )


# ---------------------------------------------------------------------------
# 异常(Task 3 起继承 TodoError,纳入统一异常层级;
# 通过 alias 保留 `status.StatusGuardError` 的导入路径稳定 — Task 2 测试不变)
# ---------------------------------------------------------------------------


class StatusGuardError(_StatusGuardError):
    """状态守卫拒绝转移时抛出(组件 3)。

    Task 3 起继承 `TodoError`(通过 `cc_harness.project.exceptions.StatusGuardError`)。
    """