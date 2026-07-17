"""Sub-project A Agent tools(spec 组件 7) + Sub-project B Task 3 第 8 个 tool。

8 个 OpenAI function-calling 工具,handler 签名一致:

    async def xxx_handler(args: dict, *, service, session_id, cwd) -> ToolResult

注入路径:`cc_harness/project/extras.py:inject_todo_tools()` 装入 deps dict,
由 `run_turn(extra_native_specs=[...])` 在 dispatch 时把 cwd 与 deps merge 成
kwargs 调用 handler。`session_id` 来自 deps(显式,不靠 env var)— 写 active_sessions
用。`cwd` 当前未用(handler 暂不读 path),保留签名以便未来 path 归一化。

错误处理原则:handler 永不让异常冒泡出 mcp dispatch(LLM 必须看到结构化 ToolResult);
所有 `TodoError` 子类在 except 块转 `ToolResult.error(display=str(e), llm=f"[xxx]
✗ {type(e).__name__}: {e}")`。`display` 给人看、`llm` 给模型看 — 但本批
handler 业务错都是开发者自用,所以两字段同内容即可。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from cc_harness.mcp_client import ToolResult
from cc_harness.project.dependency import (
    DependencyCycleError,
    get_ready_tasks,
    topo_sort,
)
from cc_harness.project.exceptions import TodoError
from cc_harness.project.models import TodoTask, ValidationIssue

# ---------------------------------------------------------------------------
# 状态/排序常量
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[str, str] = {
    "done": "✓",
    "in_progress": "⠋",
    "pending": "○",
    "blocked": "!",
    "cancelled": "✗",
}

# spec 组件 7 line 462-470:状态优先级(越小越靠前)
_STATUS_PRIORITY: dict[str, int] = {
    "in_progress": 0,
    "blocked": 1,
    "pending": 2,
    "cancelled": 3,
    "done": 4,
}

# spec 组件 7 line 495:limit 上限(防爆 context window)
_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 100


# ---------------------------------------------------------------------------
# 8 个 SPEC dict(OpenAI function-calling format)
# ---------------------------------------------------------------------------


# 全部 SPEC 列表(供 inject_todo_tools / 测试用 — 单一来源真相)。
# B 阶段 Task 3 起从 7 → 8 项。
ALL_SPECS: list[dict[str, Any]] = []  # forward declaration,后续 append


TODO_LIST_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_list",
        "description": (
            "列出 todo 任务。可按 status/parent_task 过滤;默认 limit=20 "
            "(防爆 context window,显式传 limit=N 可扩,封顶 100);"
            "默认按 status 优先级(in_progress > blocked > pending > cancelled > done)"
            " + updated_at desc 排序。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked", "cancelled"],
                    "description": "可选,按 status 过滤",
                },
                "parent_task": {
                    "type": "string",
                    "description": "可选,按 parent_task 过滤(子任务查询)",
                },
                "include_done": {
                    "type": "boolean",
                    "default": True,
                    "description": "默认 True;False 时排除 status=='done' 的任务",
                },
                "limit": {
                    "type": "integer",
                    "default": _DEFAULT_LIST_LIMIT,
                    "minimum": 1,
                    "maximum": _MAX_LIST_LIMIT,
                    "description": "返回数量上限(默认 20,封顶 100)",
                },
                "sort": {
                    "type": "string",
                    "enum": ["status", "updated_at", "created_at", "priority"],
                    "description": (
                        "排序键,默认 'status'(in_progress > blocked > pending > "
                        "cancelled > done,组内 updated_at desc)。其他值则按字段时间戳 desc,"
                        "'priority' 按 high > medium > low > critical > None,"
                        "组内 updated_at desc。"
                    ),
                },
            },
            "required": [],
        },
    },
}

TODO_GET_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_get",
        "description": "按 id 取单个 task 详情(id/title/status/depends_on/parent_task/...全字段)。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 id(8 hex 短码)"},
            },
            "required": ["task_id"],
        },
    },
}

TODO_CREATE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_create",
        "description": (
            "创建新 task。title 必填;可选 description/depends_on/parent_task/"
            "assigned_to/priority/labels/due_date/effort_estimate/acceptance_criteria。"
            "id/created_at/updated_at/active_sessions 系统自动生成。"
            "前置校验:depends_on/parent_task 引用必须存在;子图环检测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题(必填)"},
                "description": {"type": "string", "default": "", "description": "markdown 描述"},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "依赖的上游 task id 列表",
                },
                "parent_task": {"type": "string", "description": "父 task id(HTN 嵌套)"},
                "assigned_to": {"type": "string", "description": "负责人"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
                "labels": {"type": "array", "items": {"type": "string"}},
                "due_date": {
                    "type": "string",
                    "description": "ISO 8601 截止时间(如 2026-08-01T00:00:00)",
                },
                "effort_estimate": {"type": "number", "description": "工作量估计"},
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "验收标准列表",
                },
            },
            "required": ["title"],
        },
    },
}

TODO_UPDATE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_update",
        "description": (
            "更新 task 字段。可传任意 T11 字段(title/status/description/depends_on/"
            "parent_task/assigned_to/priority/labels/due_date/effort_estimate/"
            "acceptance_criteria),未传字段不动。前置校验:status_guard;depends_on "
            "引用 + 子图环;parent_task 不能 self + 引用必须存在。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 id"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked", "cancelled"],
                },
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "parent_task": {"type": ["string", "null"], "description": "null 表示清空"},
                "assigned_to": {"type": ["string", "null"]},
                "priority": {
                    "type": ["string", "null"],
                    "enum": ["low", "medium", "high", "critical", None],
                },
                "labels": {"type": "array", "items": {"type": "string"}},
                "due_date": {"type": ["string", "null"]},
                "effort_estimate": {"type": ["number", "null"]},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id"],
        },
    },
}

TODO_DELETE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_delete",
        "description": (
            "删除 task。force=False 时拒绝删除 done 状态或有 dependents 的 task;"
            "force=True 强制删除(保留 dangling references,由 todo_validate 兜底报"
            " missing_dependency)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 id"},
                "force": {"type": "boolean", "default": False, "description": "强制删除"},
            },
            "required": ["task_id"],
        },
    },
}

TODO_RESOLVE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_resolve",
        "description": (
            "BFS 解析 task 的全部上游依赖链(target + 全部传递 dep)。"
            "include_done=False 排除 status=='done' 的中间节点。"
            "返回层级缩进列表 + Ready 状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "目标任务 id"},
                "include_done": {
                    "type": "boolean",
                    "default": True,
                    "description": "默认 True;False 排除 done 中间节点",
                },
            },
            "required": ["task_id"],
        },
    },
}

TODO_VALIDATE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_validate",
        "description": (
            "全表校验:引用完整性(missing_dependency/missing_parent/self_parent) + "
            "环检测 + md/yaml 一致性(orphan_md/missing_md)。"
            "返回所有 issue 列表;strict=True 把 warning 提升为 error(只看 error 决定成败)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "strict": {
                    "type": "boolean",
                    "default": False,
                    "description": "True 时把 warning issue 提升为 error",
                },
            },
            "required": [],
        },
    },
}


# B 阶段 Task 3(组件 3):todo_toposort
TODO_TOPOSORT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo_toposort",
        "description": (
            "查看项目任务 DAG 的拓扑视图。"
            "返回全表拓扑序 + 当前 ready/in_progress/blocked 分组。"
            "用于 LLM 编排决策:'下一步做哪个?'。"
            "注: ready 指 pending 且依赖全 done,由 get_ready_tasks 计算。"
            "存在环时 is_error=True 并报告环路径,不抛异常。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "enum": ["all", "ready", "in_progress", "blocked"],
                    "default": "all",
                },
            },
            "required": [],
        },
    },
}


# 把所有 SPEC 装进 ALL_SPECS(单一来源真相)。
# forward declaration 在文件头先声明空 list,这里一次性 populate,
# 让 ALL_SPECS 永远等于 已声明的 SPEC 总和。
ALL_SPECS.extend([
    TODO_LIST_SPEC,
    TODO_GET_SPEC,
    TODO_CREATE_SPEC,
    TODO_UPDATE_SPEC,
    TODO_DELETE_SPEC,
    TODO_RESOLVE_SPEC,
    TODO_VALIDATE_SPEC,
    TODO_TOPOSORT_SPEC,  # B 阶段 Task 3: 第 8 个 tool
])
assert len(ALL_SPECS) == 8, f"ALL_SPECS 应当有 8 项,实际 {len(ALL_SPECS)}"


# ---------------------------------------------------------------------------
# handler 公共 helpers
# ---------------------------------------------------------------------------


def _format_task_line(task: TodoTask) -> str:
    """单行 task 渲染(spec line 462-470)。"""
    icon = _STATUS_ICON.get(task.status, "?")
    prio = f"[{task.priority}]" if task.priority else ""
    return f"{icon} {task.id}  {task.title}{'  ' + prio if prio else ''}  [{task.status}]"


def _parse_iso_or_none(value: Any) -> datetime | None:
    """解析 ISO 8601 字符串 → datetime;None/空/不可解析 → None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _err(tool_name: str, e: TodoError) -> ToolResult:
    """TodoError → ToolResult.error。LLM 字段含 type 名以便学习。"""
    msg = f"[{tool_name}] ✗ {type(e).__name__}: {e}"
    return ToolResult.error(display=str(e), llm=msg)


# ---------------------------------------------------------------------------
# 7 个 handler
# ---------------------------------------------------------------------------


async def todo_list_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_list:列出 + 过滤 + 排序 + limit 截断。

    sort='status' (默认) → 状态优先级组内 updated_at desc;
    其他值按字段时间戳 desc;'priority' 按 high > medium > low > critical > None。
    limit 封顶 _MAX_LIST_LIMIT(100)。
    """
    del cwd, last_turn_text  # 当前未用,保留签名
    status_filter = args.get("status")
    parent_filter = args.get("parent_task")
    include_done = args.get("include_done", True)
    limit = min(int(args.get("limit") or _DEFAULT_LIST_LIMIT), _MAX_LIST_LIMIT)
    sort_key = args.get("sort") or "status"

    try:
        tasks = await service.list(
            status=status_filter,
            parent_task=parent_filter,
            include_done=include_done,
        )
    except TodoError as e:
        return _err("todo_list", e)

    # 排序
    if sort_key == "status":
        tasks.sort(key=lambda t: (
            _STATUS_PRIORITY.get(t.status, 99),
            -t.updated_at.timestamp(),
        ))
    elif sort_key == "priority":
        prio_order = {"high": 0, "medium": 1, "low": 2, "critical": 3}
        tasks.sort(key=lambda t: (
            prio_order.get(t.priority, 4) if t.priority is not None else 4,
            -t.updated_at.timestamp(),
        ))
    elif sort_key == "created_at":
        tasks.sort(key=lambda t: -t.created_at.timestamp())
    else:  # updated_at (default fallback)
        tasks.sort(key=lambda t: -t.updated_at.timestamp())

    truncated = len(tasks) > limit
    shown = tasks[:limit]

    # 头部计数复用已按 status/parent/include_done 过滤的列表,避免二次读盘,
    # 也确保 header 与实际返回范围一致。
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    header = (
        f"[todo_list] {len(tasks)} tasks "
        f"({by_status.get('done', 0)} done / "
        f"{by_status.get('in_progress', 0)} in_progress / "
        f"{by_status.get('pending', 0)} pending)"
    )

    body = "\n".join(_format_task_line(t) for t in shown)
    suffix = f"\n# (+{len(tasks) - limit} more, narrow with status= or limit=N)" if truncated else ""
    text = f"{header}\n{body}{suffix}\n" if shown else f"{header}\n"
    return ToolResult.success(text)


async def todo_get_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_get:返回 task 全字段详情 + 触发 task 的依赖关系。"""
    del cwd, session_id, last_turn_text
    task_id = args.get("task_id")
    if not task_id:
        return ToolResult.error(
            display="task_id is required",
            llm="[todo_get] ✗ InvalidFieldError: task_id is required",
        )
    try:
        task = await service.get(task_id)
    except TodoError as e:
        return _err("todo_get", e)

    lines = [
        f"[todo_get] {task.id}",
        f"title:    {task.title}",
        f"status:   {task.status}",
        f"priority: {task.priority or '(none)'}",
        f"depends_on:    {task.depends_on or '(none)'}",
        f"parent_task:   {task.parent_task or '(none)'}",
        f"assigned_to:   {task.assigned_to or '(none)'}",
        f"labels:        {task.labels or '(none)'}",
        f"due_date:      {task.due_date.isoformat() if task.due_date else '(none)'}",
        f"effort_estimate: {task.effort_estimate if task.effort_estimate is not None else '(none)'}",
        f"acceptance_criteria: {task.acceptance_criteria or '(none)'}",
        f"created_at:    {task.created_at.isoformat()}",
        f"updated_at:    {task.updated_at.isoformat()}",
        f"active_sessions: {task.active_sessions}",
        f"description:   {task.description[:200]}{'...' if len(task.description) > 200 else ''}",
    ]
    return ToolResult.success("\n".join(lines) + "\n")


async def todo_create_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_create:T11 全字段 → Service.create。"""
    del cwd, last_turn_text
    title = args.get("title")
    if not title:
        return ToolResult.error(
            display="title is required",
            llm="[todo_create] ✗ InvalidFieldError: title is required",
        )
    try:
        task = await service.create(
            title=title,
            description=args.get("description", ""),
            depends_on=args.get("depends_on") or None,
            parent_task=args.get("parent_task") or None,
            assigned_to=args.get("assigned_to") or None,
            priority=args.get("priority") or None,
            labels=args.get("labels") or None,
            due_date=_parse_iso_or_none(args.get("due_date")),
            effort_estimate=args.get("effort_estimate"),
            acceptance_criteria=args.get("acceptance_criteria") or None,
            session_id=session_id,
        )
    except TodoError as e:
        return _err("todo_create", e)

    text = (
        f"[todo_create] ✓ created task {task.id}\n"
        f"title:    {task.title}\n"
        f"status:   {task.status}\n"
        f"id:       {task.id}\n"
    )
    return ToolResult.success(text)


async def todo_update_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_update:任意 T11 字段 → Service.update。session_id 显式传。"""
    del cwd, last_turn_text  # Task 3 将接入完成门 acceptance 校验
    task_id = args.get("task_id")
    if not task_id:
        return ToolResult.error(
            display="task_id is required",
            llm="[todo_update] ✗ InvalidFieldError: task_id is required",
        )
    # 提取字段并做类型转换(datetime)
    fields: dict[str, Any] = {}
    for k in (
        "title", "description", "status", "depends_on", "parent_task",
        "assigned_to", "priority", "labels", "acceptance_criteria",
    ):
        if k in args and args[k] is not None:
            v = args[k]
            # 空字符串当 None(等价"清空"语义)
            if isinstance(v, str) and v == "" and k in ("parent_task", "assigned_to"):
                v = None
            fields[k] = v
    if "due_date" in args:
        due = _parse_iso_or_none(args.get("due_date"))
        if due is not None or "due_date" in args:
            fields["due_date"] = due
    if "effort_estimate" in args and args["effort_estimate"] is not None:
        fields["effort_estimate"] = args["effort_estimate"]
    try:
        updated = await service.update(task_id, session_id=session_id, **fields)
    except TodoError as e:
        return _err("todo_update", e)

    # 列出本次实际变更的字段(LLM 友好反馈)
    changed = ", ".join(fields.keys()) if fields else "(no-op)"
    text = (
        f"[todo_update] ✓ updated task {updated.id}\n"
        f"status:   {updated.status}\n"
        f"changed:  {changed}\n"
    )
    return ToolResult.success(text)


async def todo_delete_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_delete:force 透传;返回简化确认。"""
    del cwd, session_id, last_turn_text
    task_id = args.get("task_id")
    if not task_id:
        return ToolResult.error(
            display="task_id is required",
            llm="[todo_delete] ✗ InvalidFieldError: task_id is required",
        )
    force = bool(args.get("force", False))
    try:
        await service.delete(task_id, force=force)
    except TodoError as e:
        return _err("todo_delete", e)
    suffix = " (force=True, dangling references left)" if force else ""
    return ToolResult.success(
        f"[todo_delete] ✓ deleted task {task_id}{suffix}\n"
    )


async def todo_resolve_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_resolve:BFS 上游依赖链 + 层级缩进(spec line 472-477)。

    返回列表顺序:target 在前,随后按 BFS 深度递增列出依赖;每行带 depth 标记。
    """
    del cwd, session_id, last_turn_text
    task_id = args.get("task_id")
    if not task_id:
        return ToolResult.error(
            display="task_id is required",
            llm="[todo_resolve] ✗ InvalidFieldError: task_id is required",
        )
    include_done = bool(args.get("include_done", True))
    try:
        chain = await service.resolve(task_id, include_done=include_done)
    except TodoError as e:
        return _err("todo_resolve", e)

    # Service.resolve 的契约是 target-first BFS,与 spec 示例的 dependency-first
    # 展示顺序不同。这里保留 Service 顺序,让目标上下文稳定出现在第一行。
    # 算每个 node 的 BFS depth(以 task_id 为 0,传递 dep 按 dep_of_target 推进)
    # 直接复用 resolve 返回顺序(target + 上游),深度 = index 顺序内推;
    # 简化:list[0]=target depth=0, 其后 dep 标注相对深度。
    lines = [f"[todo_resolve] {task_id} chain ({len(chain)} tasks)"]
    for i, t in enumerate(chain):
        if i == 0:
            marker = "← target"
        else:
            marker = f"← depth {i}"
        lines.append(f"{_format_task_line(t)}  {marker}")
    # Ready 判断:链中所有非 target 都 done
    upstream = chain[1:]
    if not upstream:
        ready_line = (
            "Ready to work: no upstream."
            if include_done
            else "Ready to work: no unresolved upstream (done tasks excluded)."
        )
    elif all(t.status == "done" for t in upstream):
        ready_line = "Ready to work: all upstream done."
    else:
        remaining = [t.id for t in upstream if t.status != "done"]
        ready_line = f"Not ready: pending upstream = {remaining}"
    text = "\n".join(lines) + "\n\n" + ready_line + "\n"
    return ToolResult.success(text)


async def todo_validate_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_validate:全表 issue 列表;strict=True 把 warning 提升为 error。"""
    del cwd, session_id, last_turn_text
    strict = bool(args.get("strict", False))
    try:
        issues = await service.validate()
    except TodoError as e:
        return _err("todo_validate", e)

    if strict:
        issues = [
            ValidationIssue(
                task_id=i.task_id,
                severity="error",
                rule_id=i.rule_id,
                message=i.message,
            ) if i.severity == "warning" else i
            for i in issues
        ]

    if not issues:
        return ToolResult.success("[todo_validate] ✓ all clean (0 issues)\n")

    error_n = sum(1 for i in issues if i.severity == "error")
    warn_n = sum(1 for i in issues if i.severity == "warning")
    head = f"[todo_validate] ✗ {len(issues)} issues found ({error_n} error / {warn_n} warning):"
    body_lines = []
    for i in issues:
        where = i.task_id or "(global)"
        # rule_id 必出现 — LLM 从结构化名学习(如 missing_dependency / cycle)
        body_lines.append(
            f"  - [{i.severity}] {where} ({i.rule_id}): {i.message}"
        )
    fix_hint = (
        "\nFix with: cc-harness todo update <id> --depends-on ...  "
        "(or delete dangling md files / re-add missing tasks)"
    )
    text = head + "\n" + "\n".join(body_lines) + fix_hint + "\n"
    # 任意 error → ToolResult.error(strict 视角);否则 success(warning-only 不算错)
    has_error = any(i.severity == "error" for i in issues)
    if has_error:
        return ToolResult.error(display=text, llm=text)
    return ToolResult.success(text)


# ---------------------------------------------------------------------------
# B 阶段 Task 3(组件 3):todo_toposort — DAG 拓扑视图给主 agent 决策用
# ---------------------------------------------------------------------------

# 截断阈值:超过此数则在输出顶端 prepend ⚠ 警告(防 context window 爆)。
MAX_RENDER_TASKS = 50


async def todo_toposort_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
) -> ToolResult:
    """todo_toposort:返回 DAG 拓扑视图。

    Args:
        args: `{"group": "all" | "ready" | "in_progress" | "blocked"}`(默认 "all")。
        service: TodoService 实例。
        session_id: 当前 session(handler 当前未使用,但保留签名)。
        cwd: 当前工作目录(handler 当前未使用)。
        last_turn_text: 上一轮 LLM 文本(handler 当前未使用,保留签名)。

    Returns:
        ToolResult:
        - 正常:is_error=False, llm_text 含 topo order + ready/in_progress/blocked/done 分组。
        - 有环:is_error=True, llm_text 含环路径(不抛异常)。
    """
    del cwd, session_id, last_turn_text
    try:
        tasks_list = await service.list(include_done=True)
    except TodoError as e:
        return _err("todo_toposort", e)

    by_id = {t.id: t for t in tasks_list}
    requested_group = args.get("group", "all")
    group = (
        requested_group
        if isinstance(requested_group, str)
        and requested_group in {"all", "ready", "in_progress", "blocked"}
        else "all"
    )

    if group == "ready":
        filtered = get_ready_tasks(by_id)
    elif group == "in_progress":
        filtered = [t for t in tasks_list if t.status == "in_progress"]
    elif group == "blocked":
        filtered = [t for t in tasks_list if t.status == "blocked"]
    else:  # "all" (default)
        filtered = tasks_list

    try:
        order = topo_sort(by_id)
        topo_error: str | None = None
    except DependencyCycleError as e:
        order = None
        topo_error = str(e)

    llm_text = _render_toposort(order, filtered, by_id, topo_error, group=group)

    if topo_error:
        return ToolResult(
            is_error=True,
            display_text=topo_error,
            llm_text=llm_text,
        )
    return ToolResult(
        is_error=False,
        display_text=f"topo: {len(order)} tasks",
        llm_text=llm_text,
    )


def _render_toposort(
    order: list[str] | None,
    filtered: list[TodoTask],
    by_id: dict[str, TodoTask],
    topo_error: str | None,
    group: str = "all",
) -> str:
    """渲染 DAG 拓扑视图给 LLM 看。

    分组渲染:Topo order / Ready / In progress / Blocked / Done。
    截断:`len(by_id) > MAX_RENDER_TASKS` 时 prepend ⚠ 行,且每个分组只展示前 50。
    注:Done 段总是简略列 id(避免长清单刷屏)。

    Args:
        order: topo_sort 输出的拓扑序;有环时为 None。
        filtered: 按 group 过滤后的 task 列表;非 all 时仅渲染对应分组。
        by_id: 全表 task 字典(handler 内部构造)。
        topo_error: 环错误信息;有环时非 None。
        group: 当前视图分组;all 渲染全表,其余值只渲染对应分组。

    Returns:
        渲染好的 LLM 可见字符串。
    """
    total = len(by_id)
    truncated = total > MAX_RENDER_TASKS

    head = ""
    if truncated:
        head = (
            f"⚠ 项目 task 数 {total} > {MAX_RENDER_TASKS}, 仅展示前 {MAX_RENDER_TASKS}\n"
        )

    if topo_error:
        topo_line = (
            f"⚠ {topo_error}\n"
            "  → 建议用 todo_update 修正 depends_on,移除或重排环路径中的依赖边\n"
        )
    elif order:
        # 截断模式:全表 id 列表可能很长,显示前 MAX + 提示
        if truncated:
            ids_str = " → ".join(order[:MAX_RENDER_TASKS]) + " → ..."
        else:
            ids_str = " → ".join(order)
        topo_line = f"Topo order: {ids_str}\n"
    else:
        topo_line = ""

    # Ready
    ready_tasks = filtered if group == "ready" else get_ready_tasks(by_id)
    ready_section = (
        f"Ready ({len(ready_tasks)}):\n"
        + "".join(
            f"  - {t.id} [pending]"
            f"{f' (priority: {t.priority})' if t.priority else ''}"
            f' "{t.title}"\n'
            for t in ready_tasks[:MAX_RENDER_TASKS]
        )
    )

    # In progress(显示 deps 状态 checkmark)
    ip_tasks = (
        filtered
        if group == "in_progress"
        else [t for t in by_id.values() if t.status == "in_progress"]
    )
    ip_lines = []
    for t in ip_tasks[:MAX_RENDER_TASKS]:
        prio = f" (priority: {t.priority})" if t.priority else ""
        deps_str = ""
        if t.depends_on:
            deps_parts: list[str] = []
            for d in t.depends_on:
                if d in by_id:
                    mark = "✓" if by_id[d].status == "done" else "✗"
                    deps_parts.append(f"{d} {mark}")
                else:
                    deps_parts.append(f"{d} ?")
            deps_str = f" (deps: {', '.join(deps_parts)})"
        ip_lines.append(f'  - {t.id} [in_progress]{prio} "{t.title}"{deps_str}\n')
    ip_section = f"In progress ({len(ip_tasks)}):\n" + "".join(ip_lines)

    # Blocked(显示 waiting on 哪些未 done 的 dep)
    bl_tasks = (
        filtered
        if group == "blocked"
        else [t for t in by_id.values() if t.status == "blocked"]
    )
    bl_lines = []
    for t in bl_tasks[:MAX_RENDER_TASKS]:
        prio = f" (priority: {t.priority})" if t.priority else ""
        waiting = ""
        if t.depends_on:
            not_done = [
                d for d in t.depends_on
                if d in by_id and by_id[d].status != "done"
            ]
            if not_done:
                waiting = f" (waiting on {', '.join(not_done)})"
        bl_lines.append(f'  - {t.id} [blocked]{prio} "{t.title}"{waiting}\n')
    bl_section = f"Blocked ({len(bl_tasks)}):\n" + "".join(bl_lines)

    # Done(简略列 id)
    done_tasks = [t for t in by_id.values() if t.status == "done"]
    done_section = f"Done ({len(done_tasks)}):\n"
    if done_tasks:
        ids_str = ", ".join(t.id for t in done_tasks[:MAX_RENDER_TASKS])
        if len(done_tasks) > MAX_RENDER_TASKS:
            n_more = len(done_tasks) - MAX_RENDER_TASKS
            done_section = f"Done ({len(done_tasks)}): {ids_str} (+{n_more} more)\n"
        else:
            done_section = f"Done ({len(done_tasks)}): {ids_str}\n"

    if group == "all":
        sections = [ready_section, ip_section, bl_section, done_section]
        header = f"DAG 拓扑视图 ({total} tasks):\n"
        filter_note = ""
    else:
        sections_by_group = {
            "ready": ready_section,
            "in_progress": ip_section,
            "blocked": bl_section,
        }
        sections = [sections_by_group[group]] if group in sections_by_group else []
        header = f"DAG 拓扑视图 ({total}, group={group}):\n"
        filter_note = f"过滤 = {group}\n"

    return "".join(
        [
            head,
            header,
            filter_note,
            ("  " + topo_line if topo_line else ""),
            "\n",
            "\n".join(sections),
        ]
    )


__all__ = [
    "TODO_LIST_SPEC", "TODO_GET_SPEC", "TODO_CREATE_SPEC", "TODO_UPDATE_SPEC",
    "TODO_DELETE_SPEC", "TODO_RESOLVE_SPEC", "TODO_VALIDATE_SPEC",
    "TODO_TOPOSORT_SPEC",  # B 阶段 Task 3
    "ALL_SPECS",
    "todo_list_handler", "todo_get_handler", "todo_create_handler",
    "todo_update_handler", "todo_delete_handler", "todo_resolve_handler",
    "todo_validate_handler",
    "todo_toposort_handler",  # B 阶段 Task 3
]