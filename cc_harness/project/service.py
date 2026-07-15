"""Sub-project A TodoService(spec 组件 2)。

7 个操作 + subscribe/unsubscribe + completion_capture 钩子。是 CLI 和
agent tools 的唯一入口,封装 storage + status_guard + dependency 校验
+ memory_bridge。

设计要点:
- 所有方法是 `async`(spec line 277)— 即便内部只是 sync I/O,保持接口稳定,
  未来可换 async storage / remote backend。
- create/update/delete 前置校验完整:引用完整性 + 依赖环 + 状态守卫 +
  parent_task 引用(Task 2 review Important 2)。
- subscribe callback 在写操作成功后调用,失败前置的 raise 不触发事件。
- _on_completion 是 self-contained:失败 swallow,仅 warn(spec line 624)。
"""
from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from cc_harness.project.dependency import (
    check_no_cycle,
    check_references,
    dep_check,
)
from cc_harness.project.exceptions import (
    InvalidFieldError,
    TaskNotFound,
    TodoError,
)
from cc_harness.project.memory_bridge import on_task_completion
from cc_harness.project.models import Manifest, TodoEvent, TodoTask, ValidationIssue
from cc_harness.project.status import status_guard
from cc_harness.project.storage import TodoStorage

if TYPE_CHECKING:
    from cc_harness.memory.service import MemoryService

log = logging.getLogger(__name__)

# subscribe callback 类型
TodoEventCallback = Callable[[TodoTask, TodoEvent], None]


class TodoService:
    """Todo CRUD + 校验 + 事件订阅的统一入口(组件 2)。

    Args:
        project_root: 项目根目录
        manifest: 项目 manifest(必填;决定 todos_path 等)
        llm: 可选 LLM client(预留,当前未使用 — B 阶段 LLM 字段提取会用到)
        memory_service: 可选 MemoryService,启用 manifest 的
            `memory.integration.completion_capture` 时用于捕获 task 完成事件
    """

    def __init__(
        self,
        project_root: Path,
        manifest: Manifest,
        llm: "object | None" = None,
        memory_service: "MemoryService | None" = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.manifest = manifest
        self.llm = llm
        self.memory_service = memory_service
        self._storage = TodoStorage(self.project_root, manifest)
        self._subscribers: list[TodoEventCallback] = []

    # ------------------------------------------------------------------ #
    # 读操作
    # ------------------------------------------------------------------ #

    async def list(
        self,
        *,
        status: str | None = None,
        parent_task: str | None = None,
        include_done: bool = True,
    ) -> list[TodoTask]:
        """列出 task。

        Args:
            status: 可选,按 status 过滤
            parent_task: 可选,按 parent_task 过滤(None 表示顶层)
            include_done: 默认 True;False 时排除 status=='done' 的 task
        """
        tasks = await self._storage.aload_all()
        out: list[TodoTask] = []
        for t in tasks:
            if status is not None and t.status != status:
                continue
            if parent_task is not None and t.parent_task != parent_task:
                continue
            if not include_done and t.status == "done":
                continue
            out.append(t)
        return out

    async def get(self, task_id: str) -> TodoTask:
        """按 id 取 task。找不到 → TaskNotFound。"""
        tasks = await self._storage.aload_all()
        for t in tasks:
            if t.id == task_id:
                return t
        raise TaskNotFound(f"task {task_id!r} not found")

    async def resolve(
        self,
        task_id: str,
        *,
        include_done: bool = True,
    ) -> list[TodoTask]:
        """BFS 解析 task 的全部上游依赖链。

        返回列表顺序:[task, *传递依赖]。如果 include_done=False,
        排除 status=='done' 的中间节点(目标 task 本身始终保留)。

        Raises:
            TaskNotFound: task_id 不存在
        """
        tasks = await self._storage.aload_all()
        by_id = {t.id: t for t in tasks}
        if task_id not in by_id:
            raise TaskNotFound(f"task {task_id!r} not found")

        target = by_id[task_id]
        visited: set[str] = set()
        ordered: list[TodoTask] = [target]
        visited.add(target.id)
        # BFS 上行 — 收集直接 dep,然后 dep 的 dep,直到稳定
        queue: deque[str] = deque(target.depends_on)

        while queue:
            dep_id = queue.popleft()
            if dep_id in visited:
                continue
            if dep_id not in by_id:
                # missing dependency — validate() 会报,这里 skip
                continue
            dep_task = by_id[dep_id]
            visited.add(dep_id)
            if not include_done and dep_task.status == "done":
                # done 节点不展开,但仍可作为线索用于 further deps
                # 实际:跳过该节点,但其 deps 也不展开(没必要)
                # spec:optional include_done=False 时排除 done 中间节点
                continue
            ordered.append(dep_task)
            queue.extend(dep_task.depends_on)

        return ordered

    async def validate(self) -> list[ValidationIssue]:
        """全表校验:引用完整性 + 环检测 + md/yaml 一致性。

        聚合所有 issue 返回;不抛异常(spec line 264)。
        """
        tasks = await self._storage.aload_all()
        by_id = {t.id: t for t in tasks}
        issues: list[ValidationIssue] = []

        # 1) 引用完整性(每个 task)
        for t in tasks:
            issues.extend(check_references(t, by_id))

        # 2) 全表环检测
        issues.extend(check_no_cycle(tasks))

        # 3) md/yaml 一致性(orphan + missing)
        issues.extend(self._storage.check_md_consistency(by_id))

        return issues

    # ------------------------------------------------------------------ #
    # 写操作 — create
    # ------------------------------------------------------------------ #

    async def create(
        self,
        *,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
        parent_task: str | None = None,
        assigned_to: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        due_date: datetime | None = None,
        effort_estimate: float | None = None,
        acceptance_criteria: list[str] | None = None,
        session_id: str | None = None,
    ) -> TodoTask:
        """新建 task。返回创建的 task。

        前置校验顺序:
        1. depends_on 中每个 id 必须存在 → TaskNotFound(Important 2)
        2. parent_task 必须存在 → TaskNotFound(Important 2)
        3. 子图环检测 → DependencyCycleError
        4. 持久化 + emit created event

        Args:
            title: 必填
            description: markdown 描述
            depends_on: 依赖的上游 task id 列表
            parent_task: 父 task id(HTN 嵌套)
            assigned_to: 负责人
            priority: low/medium/high/critical
            labels: 标签列表
            due_date: 截止时间
            effort_estimate: 工作量估计
            acceptance_criteria: 验收标准列表
            session_id: 当前 session id,会 append 到 active_sessions
        """
        if not title:
            raise InvalidFieldError("title is required")

        deps = list(depends_on or [])

        # 读全表做前置校验
        tasks = await self._storage.aload_all()
        by_id = {t.id: t for t in tasks}

        # 1) depends_on 引用必须存在(Task 2 review Important 2)
        for dep_id in deps:
            if dep_id not in by_id:
                raise TaskNotFound(
                    f"depends_on references non-existent task {dep_id!r}"
                )

        # 2) parent_task 必须存在(Important 2)
        if parent_task is not None and parent_task not in by_id:
            raise TaskNotFound(
                f"parent_task references non-existent task {parent_task!r}"
            )

        # 构造 task
        now = datetime.now(timezone.utc)
        new_id = uuid.uuid4().hex[:8]
        # 新建 task 的 active_sessions:session_id 透传,无则空列表
        sessions: list[str] = [session_id] if session_id is not None else []

        task = TodoTask(
            id=new_id,
            title=title,
            status="pending",
            description=description,
            depends_on=deps,
            parent_task=parent_task,
            assigned_to=assigned_to,
            priority=_validate_priority(priority),
            labels=list(labels or []),
            due_date=due_date,
            effort_estimate=effort_estimate,
            acceptance_criteria=list(acceptance_criteria or []),
            created_at=now,
            updated_at=now,
            active_sessions=sessions,
        )

        # 3) 子图环检测(new task 加入后)
        all_with_new = {**by_id, new_id: task}
        dep_check(new_id, deps, all_with_new)

        # 4) 持久化
        all_tasks = tasks + [task]
        await self._storage.asave_all(all_tasks)

        # 5) 事件
        self._emit(task, TodoEvent(kind="created"))

        return task

    # ------------------------------------------------------------------ #
    # 写操作 — update
    # ------------------------------------------------------------------ #

    async def update(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        **fields,
    ) -> TodoTask:
        """更新 task 字段。返回更新后的 task。

        支持任意 T11 字段(T11 字段名见 TodoTask docstring)。
        session_id 不算字段,显式 kwarg — append 到 active_sessions。

        前置校验顺序:
        1. task 存在 → 否则 TaskNotFound
        2. status in fields → status_guard
        3. depends_on in fields → check_references-equivalent(TaskNotFound)
            + dep_check(DependencyCycleError)
        4. parent_task in fields → existence check(TaskNotFound)
        5. 持久化
        6. 如果 status 非 done → done → await _on_completion(spec line 615)
        7. emit updated 或 status_changed 事件

        Raises:
            TaskNotFound, StatusGuardError, DependencyCycleError, InvalidFieldError
        """
        tasks = await self._storage.aload_all()
        by_id = {t.id: t for t in tasks}
        if task_id not in by_id:
            raise TaskNotFound(f"task {task_id!r} not found")

        task = by_id[task_id]
        prev_status = task.status

        # 1) status 守卫
        new_status = fields.get("status")
        if new_status is not None and new_status != task.status:
            status_guard(task, new_status)

        # 2) depends_on 引用 + 子图环
        new_deps = fields.get("depends_on")
        if new_deps is not None:
            new_deps = list(new_deps)
            for dep_id in new_deps:
                if dep_id == task_id:
                    raise InvalidFieldError(
                        f"task {task_id!r} cannot depend on itself"
                    )
                if dep_id not in by_id:
                    raise TaskNotFound(
                        f"depends_on references non-existent task {dep_id!r}"
                    )
            # 假设新 depends_on 应用后做子图环检测
            hypothetical = {**by_id, task_id: replace(task, depends_on=new_deps)}
            dep_check(task_id, new_deps, hypothetical)

        # 3) parent_task 引用(允许 None 表示清空)
        new_parent = fields.get("parent_task")
        if new_parent is not None:
            if new_parent == task_id:
                raise InvalidFieldError(
                    f"task {task_id!r} cannot be its own parent"
                )
            if new_parent not in by_id:
                raise TaskNotFound(
                    f"parent_task references non-existent task {new_parent!r}"
                )

        # 4) priority 校验
        if "priority" in fields and fields["priority"] is not None:
            _validate_priority(fields["priority"])

        # 5) 应用字段
        now = datetime.now(timezone.utc)
        updates = dict(fields)
        updates["updated_at"] = now
        # active_sessions — append-only(去重)
        new_sessions = list(task.active_sessions)
        if session_id is not None and session_id not in new_sessions:
            new_sessions.append(session_id)
        updates["active_sessions"] = new_sessions

        updated = replace(task, **updates)

        # 6) 持久化
        new_all = [updated if t.id == task_id else t for t in tasks]
        await self._storage.asave_all(new_all)

        # 7) completion hook(spec line 615)
        if prev_status != "done" and updated.status == "done":
            await self._on_completion(updated)

        # 8) 事件
        if new_status is not None and new_status != prev_status:
            self._emit(updated, TodoEvent(kind="status_changed", prev_status=prev_status))
        else:
            self._emit(updated, TodoEvent(kind="updated"))

        return updated

    # ------------------------------------------------------------------ #
    # 写操作 — delete
    # ------------------------------------------------------------------ #

    async def delete(self, task_id: str, *, force: bool = False) -> None:
        """删除 task。

        Args:
            task_id: 要删除的 task id
            force: 默认 False。
                - False:status=='done' 或有 dependents → 拒绝(InvalidFieldError)
                - True:强制删除,**保留 dangling references**(validate 会报
                  missing_dependency,user 自行修复)

        Raises:
            TaskNotFound: task_id 不存在
            InvalidFieldError: force=False 且拒绝条件命中
        """
        tasks = await self._storage.aload_all()
        by_id = {t.id: t for t in tasks}
        if task_id not in by_id:
            raise TaskNotFound(f"task {task_id!r} not found")

        task = by_id[task_id]

        # 检查拒绝条件
        if not force:
            if task.status == "done":
                raise InvalidFieldError(
                    f"cannot delete done task {task_id!r} without force=True"
                )
            dependents = [
                t for t in tasks
                if t.id != task_id and task_id in t.depends_on
            ]
            if dependents:
                dep_ids = ", ".join(sorted(d.id for d in dependents))
                raise InvalidFieldError(
                    f"cannot delete task {task_id!r} — has dependents: {dep_ids}. "
                    f"Use force=True to leave dangling references."
                )

        # 持久化(直接过滤)
        remaining = [t for t in tasks if t.id != task_id]
        # spec 组件 5 line 345:删 yaml 行同时删 md 文件,避免 disk orphan
        await self._storage.adelete_task_md(task_id)
        await self._storage.asave_all(remaining)

        self._emit(task, TodoEvent(kind="deleted"))

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #

    def subscribe(self, callback: TodoEventCallback) -> None:
        """注册事件回调。重复注册同一 callback 不会去重(按 list 顺序触发)。"""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: TodoEventCallback) -> None:
        """移除 callback(只删第一个匹配项)。"""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    def _emit(self, task: TodoTask, event: TodoEvent) -> None:
        """fan-out 事件到所有 subscriber。subscriber 异常 swallow(避免影响主流程)。"""
        for cb in list(self._subscribers):
            try:
                cb(task, event)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "todo event subscriber raised: %s (task=%s kind=%s)",
                    e, task.id, event.kind,
                )

    # ------------------------------------------------------------------ #
    # completion hook(私有)
    # ------------------------------------------------------------------ #

    async def _on_completion(self, task: TodoTask) -> None:
        """status 非 done → done 时调用;异常会被吞掉并记录 warning。"""
        try:
            await on_task_completion(task, self.manifest, self.memory_service)
        except Exception as e:  # noqa: BLE001 — spec 要求 swallow
            log.warning(
                "task %s completion bridge failed: %s", task.id, e,
            )


# ---------------------------------------------------------------------------
# 私有 helpers
# ---------------------------------------------------------------------------

_VALID_PRIORITIES = ("low", "medium", "high", "critical")


def _validate_priority(value: str | None) -> str | None:
    """priority 必须在 4 个枚举内,否则 InvalidFieldError。None 透传。"""
    if value is None:
        return None
    if value not in _VALID_PRIORITIES:
        raise InvalidFieldError(
            f"priority {value!r} not in {_VALID_PRIORITIES}"
        )
    return value


__all__ = ["TodoService", "TodoEventCallback", "TodoError"]