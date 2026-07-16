"""tests/test_project_tools.py — 8 个 agent tool handler + spec shape。

覆盖矩阵:
- 8 SPEC dict 结构(type/function.name/function.parameters)
- handler 成功路径:返回 ToolResult(is_error=False), llm_text 含标记
- handler 错误路径:返回 ToolResult(is_error=True), llm_text 含 type 名 + 描述
- session_id 从 deps 透传到 Service.create(append active_sessions)
- limit/sort 等参数解析行为

Handler 签名一致:`async def xxx(args: dict, *, service, session_id, cwd) -> ToolResult`,
与 run_turn 在 agent.py:228 的 dispatch 路径一致。直接调用即可测,无需 mock LLM。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cc_harness.mcp_client import ToolResult
from cc_harness.project.exceptions import TaskNotFound
from cc_harness.project.models import Manifest
from cc_harness.project.service import TodoService
from cc_harness.project.tools import (
    TODO_CREATE_SPEC,
    TODO_DELETE_SPEC,
    TODO_GET_SPEC,
    TODO_LIST_SPEC,
    TODO_RESOLVE_SPEC,
    TODO_TOPOSORT_SPEC,
    TODO_UPDATE_SPEC,
    TODO_VALIDATE_SPEC,
    todo_create_handler,
    todo_delete_handler,
    todo_get_handler,
    todo_list_handler,
    todo_resolve_handler,
    todo_toposort_handler,
    todo_update_handler,
    todo_validate_handler,
)


ALL_SPECS = [
    TODO_LIST_SPEC, TODO_GET_SPEC, TODO_CREATE_SPEC, TODO_UPDATE_SPEC,
    TODO_DELETE_SPEC, TODO_RESOLVE_SPEC, TODO_VALIDATE_SPEC,
    TODO_TOPOSORT_SPEC,  # B 阶段 Task 3
]

ALL_HANDLERS = [
    todo_list_handler, todo_get_handler, todo_create_handler,
    todo_update_handler, todo_delete_handler, todo_resolve_handler,
    todo_validate_handler, todo_toposort_handler,  # B 阶段 Task 3
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    todos = p / ".cc-harness" / "todos"
    todos.mkdir(parents=True)
    (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return p


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(
        project_id="x", name="x",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def svc(proj: Path, manifest: Manifest) -> TodoService:
    return TodoService(project_root=proj, manifest=manifest)


@pytest.fixture
def deps(svc: TodoService, proj: Path) -> dict:
    """handler deps:service/session_id/cwd。"""
    return {
        "service": svc,
        "session_id": "test-session",
        "cwd": str(proj),
    }


# ---------------------------------------------------------------------------
# SPEC shape(7 个 SPEC 必须合法 OpenAI function-calling 格式)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s["function"]["name"])
def test_spec_has_function_shape(spec):
    """每个 SPEC: type='function', function 含 name + parameters。"""
    assert spec["type"] == "function"
    fn = spec["function"]
    assert "name" in fn and isinstance(fn["name"], str)
    assert "parameters" in fn
    params = fn["parameters"]
    assert params.get("type") == "object"
    assert "properties" in params
    assert isinstance(params.get("required", []), list)


def test_specs_have_distinct_names():
    names = [s["function"]["name"] for s in ALL_SPECS]
    assert len(names) == len(set(names))
    assert set(names) == {
        "todo_list", "todo_get", "todo_create", "todo_update",
        "todo_delete", "todo_resolve", "todo_validate",
        "todo_toposort",  # B 阶段 Task 3
    }


# ---------------------------------------------------------------------------
# todo_create
# ---------------------------------------------------------------------------


async def test_create_handler_success_returns_llm_visible_text(deps):
    result = await todo_create_handler({"title": "hello"}, **deps)
    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert "[todo_create]" in result.llm_text
    assert "✓" in result.llm_text
    assert "hello" in result.llm_text
    assert "id:" in result.llm_text


async def test_create_handler_appends_session_id(deps):
    """session_id 从 deps 透传到 Service.create → active_sessions。"""
    result = await todo_create_handler({"title": "with session"}, **deps)
    assert result.is_error is False
    # 从 llm_text 抓 id,再去 svc 拿 task,确认 active_sessions 含 session_id
    # id 行形如 "id:       abc12345"
    for line in result.llm_text.splitlines():
        if line.strip().startswith("id:"):
            task_id = line.split(":", 1)[1].strip()
            break
    else:
        pytest.fail("no id line in output")
    task = await deps["service"].get(task_id)
    assert "test-session" in task.active_sessions


async def test_create_handler_missing_title_returns_error(deps):
    """空 title → InvalidFieldError → ToolResult.error 含类型名。"""
    result = await todo_create_handler({"title": ""}, **deps)
    assert result.is_error is True
    assert "InvalidFieldError" in result.llm_text
    assert "title is required" in result.llm_text


async def test_create_handler_with_invalid_priority_returns_error(deps):
    result = await todo_create_handler({"title": "x", "priority": "urgent"}, **deps)
    assert result.is_error is True
    assert "InvalidFieldError" in result.llm_text


async def test_create_handler_with_cycle_raises(deps):
    """真环 a → b → a(子图环检测)→ DependencyCycleError。"""
    a = await deps["service"].create(title="alpha")
    b = await deps["service"].create(title="bravo", depends_on=[a.id])
    # 让 a 反向依赖 b → 形成 a → b → a 的环
    result = await todo_update_handler(
        {"task_id": a.id, "depends_on": [b.id]}, **deps)
    assert result.is_error is True
    assert "DependencyCycleError" in result.llm_text
    assert "cycle" in result.llm_text.lower()


async def test_create_handler_with_missing_dep_returns_error(deps):
    result = await todo_create_handler(
        {"title": "x", "depends_on": ["ghost1234"]}, **deps)
    assert result.is_error is True
    assert "TaskNotFound" in result.llm_text


async def test_create_handler_passes_all_t11_fields(deps):
    """T11 全字段透传:description/depends_on/parent_task/assigned_to/priority/
    labels/due_date/effort_estimate/acceptance_criteria。"""
    result = await todo_create_handler({
        "title": "full",
        "description": "body",
        "priority": "high",
        "labels": ["a", "b"],
        "assigned_to": "alice",
        "effort_estimate": 2.5,
        "acceptance_criteria": ["AC1", "AC2"],
        "due_date": "2026-08-01T00:00:00",
    }, **deps)
    assert result.is_error is False
    # 从结果抓 id 拉回
    for line in result.llm_text.splitlines():
        if line.strip().startswith("id:"):
            task_id = line.split(":", 1)[1].strip()
            break
    else:
        pytest.fail("no id line")
    t = await deps["service"].get(task_id)
    assert t.priority == "high"
    assert t.labels == ["a", "b"]
    assert t.assigned_to == "alice"
    assert t.effort_estimate == 2.5
    assert t.acceptance_criteria == ["AC1", "AC2"]
    assert t.due_date is not None


# ---------------------------------------------------------------------------
# todo_get
# ---------------------------------------------------------------------------


async def test_get_handler_returns_task_full_info(deps):
    a = await deps["service"].create(title="alpha", priority="high")
    result = await todo_get_handler({"task_id": a.id}, **deps)
    assert result.is_error is False
    assert "[todo_get]" in result.llm_text
    assert a.id in result.llm_text
    assert "alpha" in result.llm_text
    assert "status:" in result.llm_text
    assert "priority:" in result.llm_text
    assert "high" in result.llm_text


async def test_get_handler_missing_returns_error(deps):
    result = await todo_get_handler({"task_id": "ghost1234"}, **deps)
    assert result.is_error is True
    assert "TaskNotFound" in result.llm_text


async def test_get_handler_missing_task_id(deps):
    result = await todo_get_handler({}, **deps)
    assert result.is_error is True
    assert "task_id is required" in result.llm_text


# ---------------------------------------------------------------------------
# todo_list
# ---------------------------------------------------------------------------


async def test_list_handler_empty(deps):
    result = await todo_list_handler({}, **deps)
    assert result.is_error is False
    assert "0 tasks" in result.llm_text


async def test_list_handler_returns_all_tasks(deps):
    for i in range(3):
        await deps["service"].create(title=f"t{i}")
    result = await todo_list_handler({}, **deps)
    assert result.is_error is False
    assert "3 tasks" in result.llm_text
    for i in range(3):
        assert f"t{i}" in result.llm_text


async def test_list_handler_limit_truncates(deps):
    for i in range(25):
        await deps["service"].create(title=f"task-{i:02d}")
    result = await todo_list_handler({"limit": 5}, **deps)
    assert result.is_error is False
    # limit 5 → 显示 5 条 + 提示 +N more
    body_lines = [
        ln for ln in result.llm_text.splitlines()
        if ln.startswith(("✓", "⠋", "○", "!", "✗"))
    ]
    assert len(body_lines) == 5
    assert "+20 more" in result.llm_text


async def test_list_handler_limit_clamped_to_100(deps):
    """limit > 100 → 截断到 100(spec line 495 防爆 context)。"""
    for i in range(120):
        await deps["service"].create(title=f"t{i}")
    result = await todo_list_handler({"limit": 999}, **deps)
    assert result.is_error is False
    body_lines = [
        ln for ln in result.llm_text.splitlines()
        if ln.startswith(("✓", "⠋", "○", "!", "✗"))
    ]
    assert len(body_lines) == 100


async def test_list_handler_status_filter(deps):
    a = await deps["service"].create(title="pending_task")
    b = await deps["service"].create(title="inprog_task")
    await deps["service"].update(b.id, status="in_progress")
    result = await todo_list_handler({"status": "pending"}, **deps)
    assert result.is_error is False
    assert "1 tasks" in result.llm_text  # header 计数与已过滤列表一致
    assert a.id in result.llm_text  # pending 出现
    assert b.id not in result.llm_text  # in_progress 被过滤


async def test_list_handler_exclude_done(deps):
    a = await deps["service"].create(title="todo_done")
    b = await deps["service"].create(title="todo_pending")
    await deps["service"].update(a.id, status="in_progress")
    await deps["service"].update(a.id, status="done")
    result = await todo_list_handler({"include_done": False}, **deps)
    assert result.is_error is False
    assert b.id in result.llm_text  # pending 出现
    assert a.id not in result.llm_text  # done 被排除


async def test_list_handler_status_priority_sort(deps):
    """默认 sort='status' → in_progress 排在 pending 之前。"""
    await deps["service"].create(title="pending_one")
    ip = await deps["service"].create(title="inprog_one")
    await deps["service"].update(ip.id, status="in_progress")
    # 手动把 updated_at 调一下,让 pending 更新更早(确保不靠 updated_at)
    result = await todo_list_handler({}, **deps)
    assert result.is_error is False
    lines = result.llm_text.splitlines()
    # 找 task id 出现顺序
    def _idx(name):
        for i, ln in enumerate(lines):
            if name in ln:
                return i
        return -1
    assert _idx("inprog_one") < _idx("pending_one")


async def test_list_handler_sort_priority_branch(deps):
    """sort='priority' 走特殊分支(不靠 status 优先级)。"""
    lo = await deps["service"].create(title="lo_prio", priority="low")
    hi = await deps["service"].create(title="hi_prio", priority="high")
    result = await todo_list_handler({"sort": "priority"}, **deps)
    assert result.is_error is False
    # high 排在 low 之前
    assert result.llm_text.find(hi.id) < result.llm_text.find(lo.id)


async def test_list_handler_sort_updated_at_branch(deps):
    """sort='updated_at' 走显式分支(默认 sort='status' 不覆盖)。"""
    a = await deps["service"].create(title="updated_test")
    result = await todo_list_handler({"sort": "updated_at"}, **deps)
    assert result.is_error is False
    assert a.id in result.llm_text


async def test_list_handler_sort_created_at_branch(deps):
    """sort='created_at' 走显式分支。"""
    a = await deps["service"].create(title="created_test")
    result = await todo_list_handler({"sort": "created_at"}, **deps)
    assert result.is_error is False
    assert a.id in result.llm_text


# ---------------------------------------------------------------------------
# todo_update
# ---------------------------------------------------------------------------


async def test_update_handler_changes_fields(deps):
    a = await deps["service"].create(title="old")
    result = await todo_update_handler(
        {"task_id": a.id, "title": "new", "priority": "low"}, **deps)
    assert result.is_error is False
    assert "✓" in result.llm_text
    assert "title" in result.llm_text  # changed 字段列表
    assert "priority" in result.llm_text


async def test_update_handler_self_parent_returns_error(deps):
    a = await deps["service"].create(title="a")
    result = await todo_update_handler(
        {"task_id": a.id, "parent_task": a.id}, **deps)
    assert result.is_error is True
    assert "InvalidFieldError" in result.llm_text


async def test_update_handler_missing_parent_returns_error(deps):
    a = await deps["service"].create(title="a")
    result = await todo_update_handler(
        {"task_id": a.id, "parent_task": "ghost1234"}, **deps)
    assert result.is_error is True
    assert "TaskNotFound" in result.llm_text


async def test_update_handler_status_guard(deps):
    """done 终态后任何转移拒绝。"""
    a = await deps["service"].create(title="a")
    await deps["service"].update(a.id, status="in_progress")
    await deps["service"].update(a.id, status="done")
    result = await todo_update_handler(
        {"task_id": a.id, "status": "pending"}, **deps)
    assert result.is_error is True
    # StatusGuardError 继承 TodoError,_err 会包含 type 名
    assert "Error" in result.llm_text  # 任意 *Error 即可


async def test_update_handler_session_id_appends(deps):
    a = await deps["service"].create(title="a")
    result = await todo_update_handler(
        {"task_id": a.id, "title": "renamed"}, **deps)
    assert result.is_error is False
    fetched = await deps["service"].get(a.id)
    assert "test-session" in fetched.active_sessions


async def test_update_handler_empty_string_clears_optional(deps):
    """空字符串 → 清空 parent_task / assigned_to(handler 内部 None 化)。"""
    a = await deps["service"].create(title="a", assigned_to="alice")
    result = await todo_update_handler(
        {"task_id": a.id, "assigned_to": ""}, **deps)
    assert result.is_error is False
    fetched = await deps["service"].get(a.id)
    assert fetched.assigned_to is None  # 空串 → None


async def test_update_handler_due_date_iso(deps):
    """due_date 接受 ISO 8601 字符串(handler 内部 parse)。"""
    a = await deps["service"].create(title="a")
    result = await todo_update_handler(
        {"task_id": a.id, "due_date": "2026-09-01T00:00:00"}, **deps)
    assert result.is_error is False
    fetched = await deps["service"].get(a.id)
    assert fetched.due_date is not None


async def test_update_handler_due_date_invalid_falls_back_to_none(deps):
    """无效 ISO 字符串 → parse 返回 None(handler 不应崩)。"""
    a = await deps["service"].create(title="a")
    result = await todo_update_handler(
        {"task_id": a.id, "due_date": "not-a-date"}, **deps)
    # InvalidFieldError (None 透传给 update) - 因为 date 是 None,update 不动它
    # 实际上 parse 返回 None → 不进 fields → 等价 no-op → success
    assert result.is_error is False


# ---------------------------------------------------------------------------
# todo_delete
# ---------------------------------------------------------------------------


async def test_delete_handler_success(deps):
    a = await deps["service"].create(title="a")
    result = await todo_delete_handler({"task_id": a.id}, **deps)
    assert result.is_error is False
    assert "✓" in result.llm_text
    assert a.id in result.llm_text
    with pytest.raises(TaskNotFound):
        await deps["service"].get(a.id)


async def test_delete_handler_with_dependents_rejected(deps):
    """force=False:有 dependents → InvalidFieldError。"""
    a = await deps["service"].create(title="a")
    await deps["service"].create(title="b", depends_on=[a.id])
    result = await todo_delete_handler({"task_id": a.id}, **deps)
    assert result.is_error is True
    assert "InvalidFieldError" in result.llm_text
    assert "dependents" in result.llm_text.lower()


async def test_delete_handler_force_succeeds(deps):
    a = await deps["service"].create(title="a")
    result = await todo_delete_handler({"task_id": a.id, "force": True}, **deps)
    assert result.is_error is False
    assert "force=True" in result.llm_text


async def test_delete_handler_done_without_force_rejected(deps):
    a = await deps["service"].create(title="a")
    await deps["service"].update(a.id, status="in_progress")
    await deps["service"].update(a.id, status="done")
    result = await todo_delete_handler({"task_id": a.id}, **deps)
    assert result.is_error is True
    assert "InvalidFieldError" in result.llm_text


async def test_delete_handler_missing_task_id(deps):
    result = await todo_delete_handler({}, **deps)
    assert result.is_error is True
    assert "task_id is required" in result.llm_text


# ---------------------------------------------------------------------------
# todo_resolve
# ---------------------------------------------------------------------------


async def test_resolve_handler_returns_chain(deps):
    a = await deps["service"].create(title="a")
    b = await deps["service"].create(title="b", depends_on=[a.id])
    c = await deps["service"].create(title="c", depends_on=[b.id])
    result = await todo_resolve_handler({"task_id": c.id}, **deps)
    assert result.is_error is False
    assert "[todo_resolve]" in result.llm_text
    assert c.id in result.llm_text
    assert b.id in result.llm_text
    assert a.id in result.llm_text
    assert "3 tasks" in result.llm_text
    # Ready: all upstream done? No — c 本身 in_progress,b/a 都 pending
    assert "Not ready" in result.llm_text


async def test_resolve_handler_all_done_ready(deps):
    a = await deps["service"].create(title="a")
    b = await deps["service"].create(title="b", depends_on=[a.id])
    await deps["service"].update(a.id, status="in_progress")
    await deps["service"].update(a.id, status="done")
    await deps["service"].update(b.id, status="in_progress")
    await deps["service"].update(b.id, status="done")
    result = await todo_resolve_handler({"task_id": b.id}, **deps)
    assert result.is_error is False
    # target 自身 done,上游也 done → "no upstream" (target 是 b,a 已 done 被排除?)
    # 实际上 chain=[b, a],all done → "all upstream done"
    assert "Ready" in result.llm_text


async def test_resolve_handler_no_deps(deps):
    a = await deps["service"].create(title="lonely")
    result = await todo_resolve_handler({"task_id": a.id}, **deps)
    assert result.is_error is False
    assert "1 tasks" in result.llm_text
    assert "no upstream" in result.llm_text


async def test_resolve_handler_missing(deps):
    result = await todo_resolve_handler({"task_id": "ghost1234"}, **deps)
    assert result.is_error is True
    assert "TaskNotFound" in result.llm_text


async def test_resolve_handler_excludes_done(deps):
    """include_done=False:done 中间节点不展开。"""
    a = await deps["service"].create(title="a")
    b = await deps["service"].create(title="b", depends_on=[a.id])
    await deps["service"].update(a.id, status="in_progress")
    await deps["service"].update(a.id, status="done")
    result = await todo_resolve_handler(
        {"task_id": b.id, "include_done": False}, **deps)
    assert result.is_error is False
    assert b.id in result.llm_text
    assert a.id not in result.llm_text  # done 被排除
    assert "no unresolved upstream (done tasks excluded)" in result.llm_text


# ---------------------------------------------------------------------------
# todo_validate
# ---------------------------------------------------------------------------


async def test_validate_handler_clean(deps):
    await deps["service"].create(title="a")
    result = await todo_validate_handler({}, **deps)
    assert result.is_error is False
    assert "✓" in result.llm_text
    assert "0 issues" in result.llm_text


async def test_validate_handler_finds_md_issues(deps, proj):
    """md 不一致 → issue 列出(I-1 修复)。"""
    t = await deps["service"].create(title="with md")
    # 手动删 md → missing_md
    (proj / ".cc-harness" / "todos" / f"{t.id}.md").unlink()
    # 加 ghost md → orphan_md
    (proj / ".cc-harness" / "todos" / "ghost1234.md").write_text(
        "---\nid: ghost1234\n---\n\norphan\n", encoding="utf-8"
    )
    result = await todo_validate_handler({}, **deps)
    # warning only → ToolResult.success(看上面 handler strict=False 行为)
    assert result.is_error is False
    assert "missing_md" in result.llm_text
    assert "orphan_md" in result.llm_text
    assert "warning" in result.llm_text


async def test_validate_handler_strict_promotes_warnings_to_errors(deps, proj):
    """strict=True → warning 提升为 error → ToolResult.error。"""
    t = await deps["service"].create(title="x")
    (proj / ".cc-harness" / "todos" / f"{t.id}.md").unlink()
    result = await todo_validate_handler({"strict": True}, **deps)
    assert result.is_error is True
    # 在 strict 视角下 missing_md 应被标为 error
    assert "[error]" in result.llm_text
    assert "missing_md" in result.llm_text


async def test_validate_handler_finds_error_level(deps):
    """强制构造 missing_dependency(外部改 yaml 模拟 force-delete 残留)→ error。"""
    a = await deps["service"].create(title="a")
    # 外部 yaml 直改
    import yaml
    yaml_path = deps["service"].project_root / ".cc-harness" / "todos" / "todos.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for entry in data["tasks"]:
        if entry["id"] == a.id:
            entry["depends_on"] = ["ghost1234"]
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    result = await todo_validate_handler({}, **deps)
    assert result.is_error is True
    assert "[error]" in result.llm_text
    assert "missing_dependency" in result.llm_text


# ---------------------------------------------------------------------------
# 错误路径兜底:所有 handler 必须返 ToolResult,绝不冒泡
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler", ALL_HANDLERS, ids=lambda h: h.__name__)
async def test_handlers_return_tool_result_on_invalid_input(handler, deps):
    """所有 handler 在异常输入下返 ToolResult(不冒泡)。"""
    # 喂一个显然错误的 args:即使 handler 不识别字段,也不应让 Python 异常逃出
    try:
        result = await handler({"__bogus__": True}, **deps)
    except Exception as e:
        pytest.fail(
            f"{handler.__name__} raised {type(e).__name__}: {e}; "
            "must return ToolResult on bad input"
        )
    assert isinstance(result, ToolResult)