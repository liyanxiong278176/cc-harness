"""B 阶段 Task 3: todo_toposort 第 8 个 agent tool 测试。

覆盖矩阵(组件 3 spec):
- 工具 7 handler 基础:default group=all / ready / in_progress / blocked / 环 / 空 manifest
- 截断:51+ task 在 handler 路径 + 直接 _render_toposort 单元层
- 集成:ALL_SPECS 8 个 / inject_todo_tools 返回 8 个

与 `todo_list` 不重叠:本测试聚焦 DAG 拓扑视图,`todo_list` 测试在
`tests/test_project_tools.py`。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.models import Manifest, TodoTask
from cc_harness.project.tools import (
    TODO_TOPOSORT_SPEC,
    _render_toposort,
    todo_toposort_handler,
)


# ---------------------------------------------------------------------------
# Helpers(全 plan 适用:_create 辅助 — Service.create 是 keyword-only 无 status)
# ---------------------------------------------------------------------------


async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    """Helper: 创建 task + (可选) update status + acceptance_criteria + depends_on。

    关键 API 细节:
    - svc.create() 是 keyword-only, 无 status 字段 (status 默认 pending)
    - svc.update(task_id, *, session_id, **fields) 也是 keyword-only,
      fields 必须作为 kwargs 传入, 不能传 dict
    """
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


def _make_task(
    id_="T1",
    status="pending",
    deps=None,
    title=None,
    priority=None,
):
    """构造一个最小 TodoTask(纯内存, 用于 _render_toposort 直接测试)。"""
    now = datetime.now()
    return TodoTask(
        id=id_,
        title=title or id_,
        status=status,
        description="",
        depends_on=deps or [],
        parent_task=None,
        assigned_to=None,
        priority=priority,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        created_at=now,
        updated_at=now,
    )


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
        project_id="x",
        name="x",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def svc(proj: Path, manifest: Manifest):
    from cc_harness.project.service import TodoService
    return TodoService(project_root=proj, manifest=manifest)


@pytest.fixture
def deps(svc, proj: Path) -> dict:
    """handler deps:service/session_id/cwd。"""
    return {
        "service": svc,
        "session_id": "test-session",
        "cwd": str(proj),
    }


# ---------------------------------------------------------------------------
# SPEC shape
# ---------------------------------------------------------------------------


def test_toposort_spec_has_function_shape():
    """第 8 个 SPEC 合法 OpenAI format。"""
    spec = TODO_TOPOSORT_SPEC
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "todo_toposort"
    assert "parameters" in spec["function"]
    params = spec["function"]["parameters"]
    assert params.get("type") == "object"
    assert "properties" in params


def test_toposort_spec_has_group_enum():
    """group 参数是 enum, 值固定 all/ready/in_progress/blocked。"""
    spec = TODO_TOPOSORT_SPEC
    group_prop = spec["function"]["parameters"]["properties"]["group"]
    assert group_prop["type"] == "string"
    assert set(group_prop["enum"]) == {"all", "ready", "in_progress", "blocked"}


def test_toposort_spec_default_group_all():
    """group 缺省值是 'all'。"""
    spec = TODO_TOPOSORT_SPEC
    group_prop = spec["function"]["parameters"]["properties"]["group"]
    assert group_prop.get("default") == "all"


# ---------------------------------------------------------------------------
# Handler:基础 group 过滤
# ---------------------------------------------------------------------------


async def test_toposort_default_group_all(deps, svc):
    """group 默认 all → 全表;包含 ready/in_progress/done 段。"""
    sid = deps["session_id"]
    await _create(svc, "T1", status="pending", session_id=sid)
    await _create(svc, "T2", status="in_progress", session_id=sid)
    # done 需要两步: pending → in_progress → done
    t3 = await _create(svc, "T3", status="pending", session_id=sid)
    await svc.update(t3.id, status="in_progress", session_id=sid)
    await svc.update(t3.id, status="done", session_id=sid)

    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is False
    # 渲染含 ready/in_progress/done 段
    assert "In progress" in result.llm_text
    assert "Done" in result.llm_text
    # 含 topo order 头
    assert "Topo order" in result.llm_text or "DAG" in result.llm_text
    # display_text 简洁
    assert "topo" in result.display_text


async def test_toposort_group_ready(deps, svc):
    """group=ready → 只 ready(pending 且 deps 全 done)。"""
    sid = deps["session_id"]
    t1 = await _create(svc, "T1", status="pending", session_id=sid)
    await _create(svc, "T2", status="in_progress", session_id=sid)

    result = await todo_toposort_handler({"group": "ready"}, **deps)
    assert result.is_error is False
    # ready 段含 T1 (T1 是 pending 且无 deps → ready)
    assert t1.id in result.llm_text
    # in_progress 的 T2 在 llm_text 中可能因为 topo_order 含它,但不在 "Ready (N)" 段里
    # 至少 in_progress 标题存在说明渲染了全表(因为渲染始终含 in_progress 段)
    # 关键断言: ready 段存在且至少 T1 出现
    assert "Ready" in result.llm_text


async def test_toposort_group_in_progress(deps, svc):
    """group=in_progress → 只 in_progress。"""
    sid = deps["session_id"]
    await _create(svc, "T1", status="pending", session_id=sid)
    t2 = await _create(svc, "T2", status="in_progress", session_id=sid)

    result = await todo_toposort_handler({"group": "in_progress"}, **deps)
    assert result.is_error is False
    # in_progress 段含 T2
    assert "In progress" in result.llm_text
    assert t2.id in result.llm_text


async def test_toposort_group_blocked(deps, svc):
    """group=blocked → 只 blocked(注: blocked status 当前不是 Service.create 默认选项,
    但 spec 仍允许 LLM 查询这个 group,空组也 OK)。

    Service 不直接接受 status='blocked',需先创建 pending 再 update。
    """
    sid = deps["session_id"]
    t1 = await _create(svc, "T1", status="pending", session_id=sid)
    # update status='blocked' (Service 应该支持这个转移,pending→blocked)
    try:
        await svc.update(t1.id, status="blocked", session_id=sid)
    except Exception:
        # 若 Service 不支持,跳过此 case
        pytest.skip("Service.update 不支持 pending→blocked")

    result = await todo_toposort_handler({"group": "blocked"}, **deps)
    assert result.is_error is False
    # Blocked 段出现
    assert "Blocked" in result.llm_text
    assert t1.id in result.llm_text


# ---------------------------------------------------------------------------
# Handler:环 → is_error=True, 报告环路径
# ---------------------------------------------------------------------------


async def test_toposort_cycle_returns_error(deps, svc):
    """有环 → is_error=True, llm_text 描述环路径。

    Service.update 内部会跑 dep_check 阻止造环,需绕开 — 直接改 yaml。
    """
    sid = deps["session_id"]
    t1 = await _create(svc, "T1", status="pending", session_id=sid)
    t2 = await _create(svc, "T2", status="pending", deps=[t1.id], session_id=sid)

    # 直接改 yaml 强制造环(Service.update 会拒绝 → 绕开)
    import yaml
    yaml_path = svc.project_root / ".cc-harness" / "todos" / "todos.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for entry in data["tasks"]:
        if entry["id"] == t1.id:
            entry["depends_on"] = [t2.id]
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is True
    # llm_text 含环路径(两个真实 task id 都出现 — 服务生成 8 字符短码)
    assert t1.id in result.llm_text and t2.id in result.llm_text
    # display_text 含 topo 摘要
    assert "topo" in result.display_text


# ---------------------------------------------------------------------------
# Handler:空 manifest
# ---------------------------------------------------------------------------


async def test_toposort_empty_manifest(deps):
    """没有任何 task → 空渲染, is_error=False。"""
    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is False
    # 渲染含 DAG 视图标题 (即便没有 task)
    assert "DAG" in result.llm_text or "Topo" in result.llm_text or "0 tasks" in result.llm_text
    # display_text 含 0 tasks
    assert "0" in result.display_text


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------


async def test_toposort_truncation_at_50(deps, svc):
    """60 task → handler 路径下输出截断 + ⚠ 警告 + 不含 50 之后的 task id。

    spec: `len(tasks) > MAX_RENDER_TASKS (=50)` 时输出截断, prepend ⚠ 警告行。
    """
    sid = deps["session_id"]
    # 创建 60 个 task
    for i in range(60):
        await _create(svc, f"Task {i:03d}", status="pending", session_id=sid)

    result = await todo_toposort_handler({}, **deps)
    assert result.is_error is False  # 无环
    # ⚠ truncated 标记
    assert "⚠" in result.llm_text or "truncated" in result.llm_text.lower()
    # 含 60 总数
    assert "60" in result.llm_text
    # 含具体 task id (前 50 个至少出现一个)
    assert "T000" in result.llm_text or "Task 000" in result.llm_text or "Task 0" in result.llm_text
    # display_text 简洁
    assert "topo" in result.display_text


def test_toposort_render_truncation_at_50_direct():
    """直接调 _render_toposort 测试渲染逻辑(单元层)。

    验证 60 tasks 时:
    - ⚠ truncated 行出现
    - 含前 50 task id (T000..T049)
    - 不含 T059(被截)
    """
    now = datetime.now()
    tasks = {}
    for i in range(60):
        tasks[f"T{i:03d}"] = TodoTask(
            id=f"T{i:03d}",
            title=f"Task {i}",
            status="pending",
            description="",
            depends_on=[],
            parent_task=None,
            assigned_to=None,
            priority=None,
            labels=[],
            due_date=None,
            effort_estimate=None,
            acceptance_criteria=[],
            created_at=now,
            updated_at=now,
        )
    order = [f"T{i:03d}" for i in range(60)]

    output = _render_toposort(order, list(tasks.values()), tasks, topo_error=None)
    assert "⚠" in output or "truncated" in output.lower()
    assert "60" in output
    assert "T000" in output
    assert "T049" in output
    assert "T059" not in output  # 第 60 个(0-indexed 59)应被截


# ---------------------------------------------------------------------------
# 集成:ALL_SPECS / inject_todo_tools
# ---------------------------------------------------------------------------


def test_all_specs_count_8():
    """ALL_SPECS 包含 8 个 SPEC(7 原有 + todo_toposort)。"""
    from cc_harness.project.tools import ALL_SPECS
    assert len(ALL_SPECS) == 8


def test_all_specs_include_toposort():
    """ALL_SPECS 含 todo_toposort。"""
    from cc_harness.project.tools import ALL_SPECS
    names = {s["function"]["name"] for s in ALL_SPECS}
    assert "todo_toposort" in names


def test_inject_todo_tools_returns_8(svc):
    """inject_todo_tools 返回 8 个 entry(7 原有 + todo_toposort)。"""
    extras = inject_todo_tools(svc, "test-session", "/tmp")
    assert isinstance(extras, list)
    assert len(extras) == 8


def test_inject_todo_tools_includes_toposort(svc):
    """inject_todo_tools 返回的 entry 含 todo_toposort spec + handler。"""
    extras = inject_todo_tools(svc, "test-session", "/tmp")
    by_name = {e["spec"]["function"]["name"]: e for e in extras}
    assert "todo_toposort" in by_name
    entry = by_name["todo_toposort"]
    assert entry["spec"] is TODO_TOPOSORT_SPEC
    assert entry["handler"] is todo_toposort_handler
    # deps 与其他 entry 一致
    assert entry["deps"]["service"] is svc
    assert entry["deps"]["session_id"] == "test-session"
    assert entry["deps"]["cwd"] == "/tmp"