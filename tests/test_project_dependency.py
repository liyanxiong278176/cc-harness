"""依赖校验测试(spec 组件 4, lines 305-309)。

三种校验:
1. check_references — 引用完整性 + self_parent
2. check_no_cycle — DFS 全表环检测
3. dep_check — 子图环检测(update depends_on 时跑)

覆盖目标:100% line + branch coverage。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from cc_harness.project.dependency import (
    DependencyCycleError,
    check_no_cycle,
    check_references,
    dep_check,
)
from cc_harness.project.models import TodoTask


def _task(
    id: str,
    status: str = "pending",
    depends_on: list[str] | None = None,
    parent_task: str | None = None,
) -> TodoTask:
    now = datetime.now()
    return TodoTask(
        id=id,
        title=id,
        status=status,
        created_at=now,
        updated_at=now,
        description="",
        depends_on=depends_on or [],
        parent_task=parent_task,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        active_sessions=[],
    )


# ---------------------------------------------------------------------------
# check_references
# ---------------------------------------------------------------------------


def test_check_references_clean() -> None:
    """所有引用都有效 → 无 issue(parent=valid + depends_on=[])。"""
    a = _task("aaa")
    b = _task("bbb", parent_task="aaa")
    issues = check_references(b, {"aaa": a, "bbb": b})
    assert issues == []


def test_check_references_no_parent() -> None:
    """parent_task=None → 不进入 parent 检查分支。"""
    a = _task("aaa", parent_task=None)
    issues = check_references(a, {"aaa": a})
    assert issues == []


def test_check_references_missing() -> None:
    """depends_on 引用不存在的 task → missing_dependency issue。"""
    a = _task("aaa", depends_on=["ghost"])
    issues = check_references(a, {"aaa": a})
    assert any(i.rule_id == "missing_dependency" for i in issues)


def test_check_references_self_parent() -> None:
    """parent_task == self → self_parent issue。"""
    a = _task("aaa")
    a.parent_task = "aaa"
    issues = check_references(a, {"aaa": a})
    assert any(i.rule_id == "self_parent" for i in issues)


def test_check_references_missing_parent() -> None:
    """parent_task 引用不存在 → missing_parent issue。"""
    a = _task("aaa", parent_task="ghost")
    issues = check_references(a, {"aaa": a})
    assert any(i.rule_id == "missing_parent" for i in issues)


def test_check_references_valid_deps() -> None:
    """所有 depends_on 都有效 → 不 emit missing_dependency(覆盖 False 分支)。"""
    a = _task("aaa", depends_on=["bbb"])
    b = _task("bbb")
    issues = check_references(a, {"aaa": a, "bbb": b})
    assert not any(i.rule_id == "missing_dependency" for i in issues)


# ---------------------------------------------------------------------------
# check_no_cycle — DFS 全表环检测
# ---------------------------------------------------------------------------


def test_check_no_cycle_detects() -> None:
    """a → b → a 循环 → emit cycle issue(plan lines 428-432)。"""
    a = _task("aaa", depends_on=["bbb"])
    b = _task("bbb", depends_on=["aaa"])
    issues = check_no_cycle([a, b])
    assert any(i.rule_id == "cycle" for i in issues)


def test_check_no_cycle_clean() -> None:
    """无依赖 → 无 issue(plan lines 434-438)。"""
    a = _task("aaa")
    b = _task("bbb")
    issues = check_no_cycle([a, b])
    assert issues == []


def test_check_no_cycle_self_loop() -> None:
    """单节点自依赖 → emit cycle issue(DFS 也要抓住)。"""
    a = _task("aaa", depends_on=["aaa"])
    issues = check_no_cycle([a])
    assert any(i.rule_id == "cycle" for i in issues)


def test_check_no_cycle_three_node() -> None:
    """a → b → c → a 三节点环 → emit cycle issue。"""
    a = _task("aaa", depends_on=["bbb"])
    b = _task("bbb", depends_on=["ccc"])
    c = _task("ccc", depends_on=["aaa"])
    issues = check_no_cycle([a, b, c])
    assert any(i.rule_id == "cycle" for i in issues)


def test_check_no_cycle_shared_sink() -> None:
    """两个独立子图共享同一个 sink → DFS 重入时 BLACK 分支命中。"""
    a = _task("aaa", depends_on=["bbb"])
    b = _task("bbb")
    c = _task("ccc", depends_on=["bbb"])
    issues = check_no_cycle([a, b, c])
    assert issues == []


def test_check_no_cycle_with_missing_dep() -> None:
    """depends_on 含已删除的 task id → 不影响环检测(False 分支 + 跳过)。"""
    a = _task("aaa", depends_on=["ghost", "bbb"])
    b = _task("bbb")
    issues = check_no_cycle([a, b])
    assert issues == []


# ---------------------------------------------------------------------------
# dep_check — 子图环检测
# ---------------------------------------------------------------------------


def test_dep_check_blocks_subgraph_cycle() -> None:
    """添加 aaa→bbb 让 bbb→aaa 形成环 → raise(plan lines 440-444)。"""
    a = _task("aaa")
    b = _task("bbb", depends_on=["aaa"])
    with pytest.raises(DependencyCycleError):
        dep_check("aaa", ["bbb"], {"aaa": a, "bbb": b})


def test_dep_check_no_cycle_passes() -> None:
    """无环添加 → 不 raise。"""
    a = _task("aaa")
    b = _task("bbb")
    dep_check("aaa", ["bbb"], {"aaa": a, "bbb": b})  # OK, no exception


def test_dep_check_self_loop() -> None:
    """任务依赖自身 → cycle,raise。"""
    a = _task("aaa")
    with pytest.raises(DependencyCycleError):
        dep_check("aaa", ["aaa"], {"aaa": a})


def test_dep_check_shared_downstream() -> None:
    """子图有共享下游节点 → DFS 重入时 BLACK 分支命中(无环场景)。"""
    a = _task("aaa")
    b = _task("bbb", depends_on=["ccc", "ddd"])
    c = _task("ccc", depends_on=["eee"])
    d = _task("ddd", depends_on=["eee"])
    e = _task("eee")
    # aaa → bbb → (ccc, ddd) → eee,无环
    dep_check(
        "aaa",
        ["bbb"],
        {"aaa": a, "bbb": b, "ccc": c, "ddd": d, "eee": e},
    )


def test_dep_check_with_missing_new_dep() -> None:
    """new_depends_on 含不存在的 task id → 跳过(False 分支),不抛。"""
    a = _task("aaa")
    dep_check("aaa", ["ghost"], {"aaa": a})  # OK, no exception