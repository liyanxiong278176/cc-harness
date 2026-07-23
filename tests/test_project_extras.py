"""tests/test_project_extras.py — inject_todo_tools 装配测试。

只测 extras 的形状和 deps 注入,不重复测 handler 行为(handler 测试在
tests/test_project_tools.py)。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cc_harness.project.extras import inject_todo_tools
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
)


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    todos = p / ".cc-harness" / "todos"
    todos.mkdir(parents=True)
    (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return p


@pytest.fixture
def svc(proj: Path) -> TodoService:
    return TodoService(
        project_root=proj,
        manifest=Manifest(
            project_id="x", name="x",
            todos_path=".cc-harness/todos",
            created_at=datetime.now(timezone.utc),
        ),
    )


# ---------------------------------------------------------------------------
# 形状 / 数量
# ---------------------------------------------------------------------------


def test_inject_returns_eight_entries(svc: TodoService):
    """D1 Task 5:inject_todo_tools 返回 9 个 entry(8 原 todo + dispatch_subagent)。"""
    extras = inject_todo_tools(svc, "test-session", "/tmp")
    assert isinstance(extras, list)
    assert len(extras) == 9


def test_inject_entries_have_required_keys(svc: TodoService):
    extras = inject_todo_tools(svc, "test-session", "/tmp")
    for entry in extras:
        assert "spec" in entry
        assert "handler" in entry
        assert "deps" in entry
        assert callable(entry["handler"])


def test_inject_specs_are_the_eight_todo_tools(svc: TodoService):
    extras = inject_todo_tools(svc, "test-session", "/tmp")
    specs = [e["spec"] for e in extras]
    expected = [
        TODO_LIST_SPEC, TODO_GET_SPEC, TODO_CREATE_SPEC, TODO_UPDATE_SPEC,
        TODO_DELETE_SPEC, TODO_RESOLVE_SPEC, TODO_VALIDATE_SPEC,
        TODO_TOPOSORT_SPEC,  # B 阶段 Task 3
    ]
    for exp in expected:
        assert exp in specs, f"missing spec: {exp['function']['name']}"


def test_inject_handlers_correspond_to_specs(svc: TodoService):
    """每个 entry 的 handler 与 spec.name 对齐(handler 在 dispatch 路径匹配)。"""
    extras = inject_todo_tools(svc, "test-session", "/tmp")
    by_name = {e["spec"]["function"]["name"]: e["handler"] for e in extras}
    assert by_name["todo_list"] is not None
    assert by_name["todo_get"] is not None
    assert by_name["todo_create"] is not None
    assert by_name["todo_update"] is not None
    assert by_name["todo_delete"] is not None
    assert by_name["todo_resolve"] is not None
    assert by_name["todo_validate"] is not None
    assert by_name["todo_toposort"] is not None  # B 阶段 Task 3


# ---------------------------------------------------------------------------
# deps 注入(service/session_id/cwd)
# ---------------------------------------------------------------------------


def test_inject_deps_contain_service(svc: TodoService):
    extras = inject_todo_tools(svc, "sess-1", "/tmp")
    for entry in extras:
        assert entry["deps"]["service"] is svc


def test_inject_deps_contain_session_id(svc: TodoService):
    extras = inject_todo_tools(svc, "sess-abc", "/tmp")
    for entry in extras:
        assert entry["deps"]["session_id"] == "sess-abc"


def test_inject_deps_contain_cwd(svc: TodoService):
    extras = inject_todo_tools(svc, "sess-1", "/custom/cwd")
    for entry in extras:
        assert entry["deps"]["cwd"] == "/custom/cwd"


def test_inject_deps_share_same_dict_object(svc: TodoService):
    """所有 entry 共享同一 deps dict(handler 用同一 service/session/cwd)。"""
    extras = inject_todo_tools(svc, "sess-1", "/tmp")
    first_deps = extras[0]["deps"]
    for entry in extras[1:]:
        # 共享同一 service 对象引用即可(dict 对象是否同一不强求)
        assert entry["deps"]["service"] is first_deps["service"]
        assert entry["deps"]["session_id"] == first_deps["session_id"]
        assert entry["deps"]["cwd"] == first_deps["cwd"]


def test_inject_cwd_default_is_empty_string(svc: TodoService):
    """cwd 默认空字符串(handler 当前未用,签名兼容)。"""
    extras = inject_todo_tools(svc, "sess-1")
    assert extras[0]["deps"]["cwd"] == ""


# ---------------------------------------------------------------------------
# 集成:extras 直接喂给 handler 能跑通(smoke)
# ---------------------------------------------------------------------------


async def test_extras_entries_are_directly_dispatchable(svc: TodoService):
    """模拟 run_turn dispatch:用 entry['handler'](args, **entry['deps']) 调得通。"""
    extras = inject_todo_tools(svc, "sess-X", "/tmp")
    by_name = {e["spec"]["function"]["name"]: e for e in extras}
    entry = by_name["todo_create"]
    result = await entry["handler"](
        {"title": "smoke", "acceptance_criteria": ["created"]}, **entry["deps"],
    )
    # ToolResult.success
    from cc_harness.mcp_client import ToolResult
    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert "smoke" in result.llm_text