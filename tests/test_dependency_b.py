"""B 阶段 Task 1: topo_sort + get_ready_tasks 单元测试。

spec: docs/superpowers/specs/2026-07-16-b-outer-loop-dag-design.md (组件 1)
plan: docs/superpowers/plans/2026-07-16-b-outer-loop-dag-plan.md (Task 1)

覆盖目标: cc_harness/project/dependency.py 保持 100% line + branch。

TDD 边界 case:
- topo_sort: 空 / 单 / 链 / 菱形 / 并行 / 环 / 缺失依赖 / self-loop / 字典序 / done 包含 / 字典外忽略
- get_ready_tasks: 空 / pending 无依赖 / pending 依赖全 done / pending 依赖部分 done /
  in_progress 排除 / 缺失依赖不阻塞 / blocked 排除
"""
from __future__ import annotations

from datetime import datetime

import pytest

from cc_harness.project.dependency import (
    DependencyCycleError,
    get_ready_tasks,
    topo_sort,
)
from cc_harness.project.models import TodoTask


def _task(
    id: str,
    status: str = "pending",
    depends_on: list[str] | None = None,
) -> TodoTask:
    """最小 TodoTask 工厂(纯内存,无 DB)。"""
    now = datetime.now()
    return TodoTask(
        id=id,
        title=id,
        status=status,
        created_at=now,
        updated_at=now,
        description="",
        depends_on=depends_on or [],
        parent_task=None,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        active_sessions=[],
    )


# =====================================================================
# topo_sort
# =====================================================================


def test_topo_sort_empty_dict() -> None:
    """空 dict → []."""
    assert topo_sort({}) == []


def test_topo_sort_single_node() -> None:
    """单 task 无依赖 → [id]."""
    t = _task("T1")
    assert topo_sort({"T1": t}) == ["T1"]


def test_topo_sort_chain() -> None:
    """链 T1 → T2 → T3 → [T1, T2, T3]."""
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2", depends_on=["T1"]),
        "T3": _task("T3", depends_on=["T2"]),
    }
    assert topo_sort(tasks) == ["T1", "T2", "T3"]


def test_topo_sort_diamond() -> None:
    """菱形: T1 → {T2, T3} → T4.
    约束: T1 < T2 < T4, T1 < T3 < T4.
    """
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2", depends_on=["T1"]),
        "T3": _task("T3", depends_on=["T1"]),
        "T4": _task("T4", depends_on=["T2", "T3"]),
    }
    order = topo_sort(tasks)
    assert order[0] == "T1"
    assert order[-1] == "T4"
    assert order.index("T2") < order.index("T4")
    assert order.index("T3") < order.index("T4")
    assert len(order) == 4


def test_topo_sort_parallel_independent() -> None:
    """并行无依赖 → 字典序 [T1, T2, T3]."""
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2"),
        "T3": _task("T3"),
    }
    assert topo_sort(tasks) == ["T1", "T2", "T3"]


def test_topo_sort_cycle_raises() -> None:
    """环 T1 → T2 → T1 → DependencyCycleError, 消息含两个 id."""
    tasks = {
        "T1": _task("T1", depends_on=["T2"]),
        "T2": _task("T2", depends_on=["T1"]),
    }
    with pytest.raises(DependencyCycleError) as exc_info:
        topo_sort(tasks)
    msg = str(exc_info.value)
    assert msg  # 非空
    assert "T1" in msg and "T2" in msg


def test_topo_sort_missing_dep_does_not_block() -> None:
    """依赖引用不在 dict → 跳过该边, 不阻塞拓扑."""
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2", depends_on=["MISSING"]),
    }
    # MISSING 跳过 → T1 → T2 顺序
    assert topo_sort(tasks) == ["T1", "T2"]


def test_topo_sort_self_loop_raises() -> None:
    """task depends_on 含自己 → DependencyCycleError."""
    t = _task("T1", depends_on=["T1"])
    with pytest.raises(DependencyCycleError):
        topo_sort({"T1": t})


def test_topo_sort_lexicographic_tiebreaker() -> None:
    """并行无依赖节点 → 字典序 tiebreaker(不依赖 dict 插入序)."""
    # 插入序为 T2, T1, T3 → 输出应为 T1, T2, T3
    tasks = {
        "T2": _task("T2"),
        "T1": _task("T1"),
        "T3": _task("T3"),
    }
    assert topo_sort(tasks) == ["T1", "T2", "T3"]


def test_topo_sort_done_tasks_included() -> None:
    """done task 仍出现在输出(topo 排序所有 dict key)."""
    tasks = {
        "T1": _task("T1", status="done"),
        "T2": _task("T2", status="done", depends_on=["T1"]),
        "T3": _task("T3", status="pending", depends_on=["T2"]),
    }
    assert topo_sort(tasks) == ["T1", "T2", "T3"]


def test_topo_sort_uses_only_dict_keys() -> None:
    """dict 外的 dep 边被忽略, 不影响 dict 内拓扑."""
    # T2 depends on GHOST (not in dict) — ignored. T1 → T2 valid.
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2", depends_on=["T1", "GHOST"]),
    }
    assert topo_sort(tasks) == ["T1", "T2"]


# =====================================================================
# get_ready_tasks
# =====================================================================


def test_get_ready_empty_dict() -> None:
    """空 dict → []."""
    assert get_ready_tasks({}) == []


def test_get_ready_no_deps_pending() -> None:
    """pending 无依赖 → ready."""
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2"),
    }
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T1", "T2"}


def test_get_ready_deps_all_done() -> None:
    """pending 依赖全 done → ready."""
    tasks = {
        "T1": _task("T1", status="done"),
        "T2": _task("T2", depends_on=["T1"]),
    }
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T2"}


def test_get_ready_deps_partial_done() -> None:
    """pending 依赖一个 done 一个 in_progress → 不 ready."""
    tasks = {
        "T1": _task("T1", status="done"),
        "T2": _task("T2", status="in_progress"),  # 未 done
        "T3": _task("T3", depends_on=["T1", "T2"]),
    }
    ready = get_ready_tasks(tasks)
    # 只有 T1 已 done, T2 in_progress 阻塞 T3
    assert {t.id for t in ready} == set()


def test_get_ready_in_progress_excluded() -> None:
    """in_progress 不算 ready(只 pending 是)."""
    tasks = {
        "T1": _task("T1", status="in_progress"),
    }
    assert get_ready_tasks(tasks) == []


def test_get_ready_missing_dep_not_blocking() -> None:
    """依赖引用不在 dict → 不阻塞(由 validate 报告)."""
    tasks = {
        "T1": _task("T1"),
        "T2": _task("T2", depends_on=["MISSING"]),
    }
    ready = get_ready_tasks(tasks)
    assert {t.id for t in ready} == {"T1", "T2"}


def test_get_ready_blocked_excluded() -> None:
    """blocked 不算 ready."""
    tasks = {
        "T1": _task("T1", status="blocked"),
    }
    assert get_ready_tasks(tasks) == []