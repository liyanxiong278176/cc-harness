"""Sub-project A 依赖校验(spec 组件 4, lines 305-309)。

三种校验:
1. check_references(task, all_tasks) — 引用完整性 + self_parent
2. check_no_cycle(tasks) — DFS white/gray/black 全表环检测
3. dep_check(task_id, new_depends_on, all_tasks) — 子图环检测

YAGNI:不实现拓扑排序(B 阶段 Kahn 算法占位)。
"""
from __future__ import annotations

from dataclasses import replace

from cc_harness.project.exceptions import DependencyCycleError as _DependencyCycleError
from cc_harness.project.models import TodoTask, ValidationIssue


# DFS 节点颜色:未访问 / 访问中(栈上) / 已完成
_WHITE, _GRAY, _BLACK = 0, 1, 2


def check_references(
    task: TodoTask, all_tasks: dict[str, TodoTask]
) -> list[ValidationIssue]:
    """校验 task 的 depends_on 与 parent_task 引用完整性。

    Returns:
        issue 列表(可能空)。问题:
            - depends_on 中引用不存在的 task → missing_dependency(error)
            - parent_task == self → self_parent(error)
            - parent_task 引用不存在 → missing_parent(error)
    """
    issues: list[ValidationIssue] = []

    # depends_on 引用必须存在
    for dep_id in task.depends_on:
        if dep_id not in all_tasks:
            issues.append(
                ValidationIssue(
                    task_id=task.id,
                    severity="error",
                    rule_id="missing_dependency",
                    message=(
                        f"task {task.id} depends on non-existent task {dep_id!r}"
                    ),
                )
            )

    # parent_task:必须存在,不能 self
    parent = task.parent_task
    if parent is None:
        return issues
    if parent == task.id:
        issues.append(
            ValidationIssue(
                task_id=task.id,
                severity="error",
                rule_id="self_parent",
                message=f"task {task.id} cannot be its own parent",
            )
        )
    elif parent not in all_tasks:
        issues.append(
            ValidationIssue(
                task_id=task.id,
                severity="error",
                rule_id="missing_parent",
                message=(
                    f"task {task.id} references non-existent parent {parent!r}"
                ),
            )
        )

    return issues


def check_no_cycle(tasks: list[TodoTask]) -> list[ValidationIssue]:
    """全表环检测。DFS white/gray/black,O(V+E)。

    对每个 task 做 DFS;遇到 gray 节点(栈上)即发现回边 → emit cycle issue
    (含循环路径)。不抛异常,允许上层批量报告多个 cycle。

    只跟踪存在于 tasks 集合内的依赖边;depends_on 中指向已删除 task 的 id
    由 check_references 处理。

    Returns:
        cycle issue 列表(无环时空)。
    """
    issues: list[ValidationIssue] = []
    by_id: dict[str, TodoTask] = {t.id: t for t in tasks}
    color: dict[str, int] = {t.id: _WHITE for t in tasks}

    def visit(node_id: str, path: list[str]) -> None:
        c = color[node_id]
        if c == _GRAY:
            # 回边:从 path 中找到当前节点位置,构造环路径
            cycle_start = path.index(node_id)
            chain = " -> ".join(path[cycle_start:] + [node_id])
            issues.append(
                ValidationIssue(
                    task_id=node_id,
                    severity="error",
                    rule_id="cycle",
                    message=f"dependency cycle detected: {chain}",
                )
            )
            return
        if c == _BLACK:
            return  # 子树已探索完毕,无环
        color[node_id] = _GRAY
        node = by_id[node_id]
        path.append(node_id)
        for dep_id in node.depends_on:
            # 只在已知节点中跟踪 — 缺失依赖由 check_references 报告
            if dep_id in by_id:
                visit(dep_id, path)
        path.pop()
        color[node_id] = _BLACK

    for t in tasks:
        if color[t.id] == _WHITE:
            visit(t.id, [])

    return issues


def dep_check(
    task_id: str,
    new_depends_on: list[str],
    all_tasks: dict[str, TodoTask],
) -> None:
    """子图环检测。检验把 task_id 的 depends_on 改成 new_depends_on 后是否会引入环。

    用于 update(depends_on=...) 时前置校验。不会修改 all_tasks(用 dataclasses.replace
    构造 hypothetical 副本)。

    Precondition:
        task_id 必须在 all_tasks 中(由 Service.update 校验)。

    Raises:
        DependencyCycleError: 新依赖会形成环;消息含环路径。
    """
    # 构造 hypothetical 状态:把目标 task 的 depends_on 替换为 new_depends_on
    target = all_tasks[task_id]
    hypothetical: dict[str, TodoTask] = dict(all_tasks)
    hypothetical[task_id] = replace(target, depends_on=list(new_depends_on))

    color: dict[str, int] = {tid: _WHITE for tid in hypothetical}
    path: list[str] = []

    def visit(node_id: str) -> None:
        c = color[node_id]
        if c == _GRAY:
            # 回边:从 path 中找到当前节点位置,构造环路径
            cycle_start = path.index(node_id)
            chain = " -> ".join(path[cycle_start:] + [node_id])
            raise DependencyCycleError(
                f"adding depends_on would create cycle: {chain}"
            )
        if c == _BLACK:
            return
        color[node_id] = _GRAY
        node = hypothetical[node_id]
        path.append(node_id)
        for dep_id in node.depends_on:
            if dep_id in hypothetical:
                visit(dep_id)
        path.pop()
        color[node_id] = _BLACK

    visit(task_id)


# ---------------------------------------------------------------------------
# 异常(Task 3 起继承 TodoError,纳入统一异常层级;
# 通过 alias 保留 `dependency.DependencyCycleError` 的导入路径稳定 — Task 2 测试不变)
# ---------------------------------------------------------------------------


class DependencyCycleError(_DependencyCycleError):
    """子图环检测发现潜在依赖环时抛出(组件 4)。

    Task 3 起继承 `TodoError`。
    """


# ---------------------------------------------------------------------------
# 占位(YAGNI):拓扑排序 B 阶段(组件 2)再实现。
# 计划:用 Kahn 算法,输入 task 列表,返回拓扑序 list[id];失败(有环)抛 DependencyCycleError。
# ---------------------------------------------------------------------------
# TODO: B 阶段实现 Kahn 拓扑排序。