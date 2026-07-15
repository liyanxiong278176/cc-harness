"""TodoService 测试(spec 组件 2 + 7 + 10)。

覆盖矩阵:
- CRUD 7 操作
- subscribe / unsubscribe
- status_guard / cycle / reference 校验前置
- _on_completion 钩子(mock memory_bridge)
- delete(force=False) 拒绝 / delete(force=True) 留 dangling
- list 过滤 / resolve BFS / validate 聚合

Fixture 模式:tmp_path 提供 proj 根,自动建 `.cc-harness/todos/todos.yaml`。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cc_harness.project.exceptions import (
    DependencyCycleError,
    InvalidFieldError,
    TaskNotFound,
    TodoError,
)
from cc_harness.project.models import Manifest, TodoEvent, TodoTask
from cc_harness.project.service import TodoService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """标准项目根:含 `.cc-harness/todos/todos.yaml`(空索引)。"""
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
def svc(proj: Path, manifest: Manifest) -> TodoService:
    return TodoService(project_root=proj, manifest=manifest)


# ---------------------------------------------------------------------------
# create + get
# ---------------------------------------------------------------------------


async def test_create_and_get(svc: TodoService) -> None:
    t = await svc.create(title="hello")
    assert t.title == "hello"
    assert t.status == "pending"
    assert t.description == ""
    assert t.depends_on == []
    assert t.priority is None
    assert t.labels == []
    assert len(t.id) == 8  # uuid4 hex[:8]

    fetched = await svc.get(t.id)
    assert fetched.id == t.id
    assert fetched.title == "hello"


async def test_create_auto_generates_id_and_timestamps(svc: TodoService) -> None:
    t = await svc.create(title="t")
    assert isinstance(t.created_at, datetime)
    assert isinstance(t.updated_at, datetime)
    assert t.created_at == t.updated_at


async def test_create_persists_to_storage(svc: TodoService, proj: Path) -> None:
    t = await svc.create(title="persist me", description="desc")
    # reload via new service instance
    new_svc = TodoService(project_root=proj, manifest=svc.manifest)
    reloaded = await new_svc.get(t.id)
    assert reloaded.title == "persist me"
    assert reloaded.description == "desc"


async def test_create_empty_title_raises(svc: TodoService) -> None:
    with pytest.raises(InvalidFieldError, match="title is required"):
        await svc.create(title="")


async def test_create_with_session_id_appends(svc: TodoService) -> None:
    t = await svc.create(title="x", session_id="sess-A")
    assert "sess-A" in t.active_sessions
    # 二次 create 不重复
    t2 = await svc.create(title="y", session_id="sess-A")
    # t2 是新 task,active_sessions 只含 sess-A(去重)
    assert t2.active_sessions == ["sess-A"]


async def test_create_with_no_session_id_keeps_empty(svc: TodoService) -> None:
    t = await svc.create(title="x")
    assert t.active_sessions == []


# ---------------------------------------------------------------------------
# create — 引用校验(Important 2)
# ---------------------------------------------------------------------------


async def test_create_with_depends_on_validates(svc: TodoService) -> None:
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    assert a.id in b.depends_on


async def test_create_with_missing_depends_on_raises(svc: TodoService) -> None:
    """Task 2 review followup:depends_on 引用不存在 task → TaskNotFound(而非静默接受)。"""
    with pytest.raises(TaskNotFound, match="ghost"):
        await svc.create(title="b", depends_on=["ghost1234"])


async def test_create_with_missing_parent_raises(svc: TodoService) -> None:
    """Task 2 review followup:parent_task 引用不存在 → TaskNotFound。"""
    with pytest.raises(TaskNotFound, match="ghost"):
        await svc.create(title="b", parent_task="ghost1234")


async def test_create_with_valid_parent(svc: TodoService) -> None:
    a = await svc.create(title="a")
    b = await svc.create(title="b", parent_task=a.id)
    assert b.parent_task == a.id


async def test_create_blocks_self_loop_via_update(svc: TodoService) -> None:
    """create 自身不能形成自环(自依赖)— Service.update 在 depends_on=[self] 时挡。"""
    a = await svc.create(title="a")
    with pytest.raises(InvalidFieldError):
        await svc.update(a.id, depends_on=[a.id])


async def test_create_validates_priority(svc: TodoService) -> None:
    t = await svc.create(title="x", priority="high")
    assert t.priority == "high"

    with pytest.raises(InvalidFieldError):
        await svc.create(title="y", priority="urgent")


async def test_update_validates_priority(svc: TodoService) -> None:
    """update 时 priority 非法 → InvalidFieldError(覆盖 service.py:348 分支)。"""
    t = await svc.create(title="x")
    with pytest.raises(InvalidFieldError):
        await svc.update(t.id, priority="urgent")
    # 合法 priority 正常
    updated = await svc.update(t.id, priority="low")
    assert updated.priority == "low"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_empty(svc: TodoService) -> None:
    assert await svc.list() == []


async def test_list_all(svc: TodoService) -> None:
    a = await svc.create(title="a")
    b = await svc.create(title="b")
    tasks = await svc.list()
    assert {t.id for t in tasks} == {a.id, b.id}


async def test_list_filter_by_status(svc: TodoService) -> None:
    await svc.create(title="a")
    b = await svc.create(title="b")
    await svc.update(b.id, status="in_progress")
    in_prog = await svc.list(status="in_progress")
    assert {t.id for t in in_prog} == {b.id}


async def test_list_filter_by_parent(svc: TodoService) -> None:
    a = await svc.create(title="parent")
    b = await svc.create(title="child", parent_task=a.id)
    await svc.create(title="other")
    children = await svc.list(parent_task=a.id)
    assert {t.id for t in children} == {b.id}


async def test_list_exclude_done(svc: TodoService) -> None:
    a = await svc.create(title="a")
    b = await svc.create(title="b")
    await svc.update(a.id, status="in_progress")
    await svc.update(a.id, status="done")
    no_done = await svc.list(include_done=False)
    assert {t.id for t in no_done} == {b.id}


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_not_found_raises(svc: TodoService) -> None:
    with pytest.raises(TaskNotFound):
        await svc.get("ghost1234")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_status_calls_guard(svc: TodoService) -> None:
    t = await svc.create(title="x")
    # pending → in_progress → done(状态机要求过 in_progress)
    await svc.update(t.id, status="in_progress")
    updated = await svc.update(t.id, status="done")
    assert updated.status == "done"
    with pytest.raises(Exception) as exc_info:
        await svc.update(t.id, status="pending")
    assert isinstance(exc_info.value, TodoError)  # StatusGuardError → TodoError


async def test_update_done_terminal_blocks(svc: TodoService) -> None:
    """done 终态后任何转移都拒绝(StatusGuardError)。"""
    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")
    for bad in ("pending", "in_progress", "blocked", "cancelled"):
        with pytest.raises(TodoError):
            await svc.update(t.id, status=bad)


async def test_update_title_and_description(svc: TodoService) -> None:
    t = await svc.create(title="old")
    updated = await svc.update(t.id, title="new", description="d")
    assert updated.title == "new"
    assert updated.description == "d"
    assert updated.updated_at >= t.updated_at


async def test_update_with_missing_depends_on_raises(svc: TodoService) -> None:
    """Important 2:update depends_on 引用不存在 → TaskNotFound。"""
    t = await svc.create(title="x")
    with pytest.raises(TaskNotFound, match="ghost"):
        await svc.update(t.id, depends_on=["ghost1234"])


async def test_update_depends_on_creates_cycle_raises(svc: TodoService) -> None:
    """Important 2:update depends_on 引入环 → DependencyCycleError。"""
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    # 试图让 a 依赖 b → 形成环
    with pytest.raises(DependencyCycleError):
        await svc.update(a.id, depends_on=[b.id])


async def test_update_self_dependency_raises(svc: TodoService) -> None:
    t = await svc.create(title="x")
    with pytest.raises(InvalidFieldError, match="cannot depend on itself"):
        await svc.update(t.id, depends_on=[t.id])


async def test_update_self_parent_raises(svc: TodoService) -> None:
    t = await svc.create(title="x")
    with pytest.raises(InvalidFieldError, match="cannot be its own parent"):
        await svc.update(t.id, parent_task=t.id)


async def test_update_with_missing_parent_raises(svc: TodoService) -> None:
    """Important 2:update parent_task 引用不存在 → TaskNotFound。"""
    t = await svc.create(title="x")
    with pytest.raises(TaskNotFound, match="ghost"):
        await svc.update(t.id, parent_task="ghost1234")


async def test_update_unknown_field_is_allowed_via_replace(svc: TodoService) -> None:
    """未知字段:dataclasses.replace 会 TypeError — 当前 Service 不挡这个,让 LLM 调试信息更明显。

    注:这是为了避免 Service 默默吞掉 typo(如 'priorty')导致 LLM 重复试错。
    """
    t = await svc.create(title="x")
    with pytest.raises(TypeError):
        await svc.update(t.id, priorty="high")  # typo


async def test_update_session_id_appends(svc: TodoService) -> None:
    t = await svc.create(title="x")
    await svc.update(t.id, session_id="sess-A")
    fetched = await svc.get(t.id)
    assert "sess-A" in fetched.active_sessions


async def test_update_not_found_raises(svc: TodoService) -> None:
    with pytest.raises(TaskNotFound):
        await svc.update("ghost1234", title="x")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_removes_task(svc: TodoService) -> None:
    t = await svc.create(title="x")
    await svc.delete(t.id)
    with pytest.raises(TaskNotFound):
        await svc.get(t.id)


async def test_delete_done_without_force_raises(svc: TodoService) -> None:
    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")
    with pytest.raises(InvalidFieldError, match="cannot delete done task"):
        await svc.delete(t.id)


async def test_delete_done_with_force_succeeds(svc: TodoService) -> None:
    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")
    await svc.delete(t.id, force=True)
    with pytest.raises(TaskNotFound):
        await svc.get(t.id)


async def test_delete_with_dependents_without_force_raises(svc: TodoService) -> None:
    """force=False:有 dependents 拒绝删除。"""
    a = await svc.create(title="a")
    await svc.create(title="b", depends_on=[a.id])
    with pytest.raises(InvalidFieldError, match="has dependents"):
        await svc.delete(a.id)


async def test_delete_force_with_dependents_creates_dangling(svc: TodoService) -> None:
    """force=True:删除后 dependents 的 depends_on 留 dangling → validate 报 missing_dependency。"""
    a = await svc.create(title="a")
    await svc.create(title="b", depends_on=[a.id])
    await svc.delete(a.id, force=True)
    issues = await svc.validate()
    assert any(i.rule_id == "missing_dependency" for i in issues)


async def test_delete_not_found_raises(svc: TodoService) -> None:
    with pytest.raises(TaskNotFound):
        await svc.delete("ghost1234")


async def test_delete_removes_md_file(svc: TodoService, proj: Path) -> None:
    """spec 组件 5 line 345:delete task 时同步删 yaml 行 + md 文件。"""
    t = await svc.create(title="with md", description="body")
    md_path = proj / ".cc-harness" / "todos" / f"{t.id}.md"
    assert md_path.is_file(), "md file should be created by save_all"

    await svc.delete(t.id, force=True)

    assert not md_path.exists(), "md file should be removed on delete"


async def test_delete_no_force_on_done_removes_nothing(
    svc: TodoService, proj: Path,
) -> None:
    """force=False 拒绝路径:raise 发生在触碰 md 之前(防止半状态)。"""
    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")
    md_path = proj / ".cc-harness" / "todos" / f"{t.id}.md"

    with pytest.raises(InvalidFieldError):
        await svc.delete(t.id)  # no force

    # md 文件应仍存在(拒绝路径未触动 md)
    assert md_path.is_file()


async def test_delete_md_idempotent_on_missing(svc: TodoService) -> None:
    """delete 一个 md 已被人为删掉的 task 不应崩。"""
    t = await svc.create(title="x")
    # 手动删 md(模拟外部清理)
    md_path = svc.project_root / ".cc-harness" / "todos" / f"{t.id}.md"
    md_path.unlink()

    # force=True 路径应正常删除 yaml 行,不动 md(no-op)
    await svc.delete(t.id, force=True)
    assert not md_path.exists()


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


async def test_resolve_returns_self_only_when_no_deps(svc: TodoService) -> None:
    t = await svc.create(title="x")
    chain = await svc.resolve(t.id)
    assert {c.id for c in chain} == {t.id}


async def test_resolve_returns_chain(svc: TodoService) -> None:
    """BFS:target + 直接 dep + dep 的 dep。"""
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    c = await svc.create(title="c", depends_on=[b.id])
    chain = await svc.resolve(c.id)
    ids = [t.id for t in chain]
    assert c.id in ids and b.id in ids and a.id in ids
    # c 在前(最先),其余顺序由 BFS 决定
    assert ids[0] == c.id


async def test_resolve_exclude_done(svc: TodoService) -> None:
    """include_done=False:排除 done 中间节点。"""
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    await svc.update(a.id, status="in_progress")
    await svc.update(a.id, status="done")
    chain = await svc.resolve(b.id, include_done=False)
    ids = {t.id for t in chain}
    assert b.id in ids
    assert a.id not in ids  # done 被排除


async def test_resolve_not_found_raises(svc: TodoService) -> None:
    with pytest.raises(TaskNotFound):
        await svc.resolve("ghost1234")


async def test_resolve_with_missing_dep_skips(svc: TodoService) -> None:
    """dangling dep:跳过(validate 会报,这里 resolve 不抛)。

    Service.create 已拒绝 dangling,这里通过 yaml 直改制造场景(模拟外部
    编辑损坏 yaml)。
    """
    import yaml

    a = await svc.create(title="a")
    yaml_path = svc.project_root / ".cc-harness" / "todos" / "todos.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for entry in data["tasks"]:
        if entry["id"] == a.id:
            entry["depends_on"] = ["ghost1234"]
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    chain = await svc.resolve(a.id)
    assert {t.id for t in chain} == {a.id}


async def test_resolve_diamond_dedup(svc: TodoService) -> None:
    """菱形依赖 c → b → a, c → a(b 和 c 都依赖 a)— BFS 不重复处理 a。"""
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    c = await svc.create(title="c", depends_on=[b.id, a.id])
    chain = await svc.resolve(c.id)
    ids = [t.id for t in chain]
    # a 只出现一次(visited 去重)
    assert ids.count(a.id) == 1
    assert {c.id, b.id, a.id} == set(ids)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


async def test_validate_empty_returns_no_issues(svc: TodoService) -> None:
    issues = await svc.validate()
    assert issues == []


async def test_validate_clean_returns_no_issues(svc: TodoService) -> None:
    a = await svc.create(title="a")
    await svc.create(title="b", depends_on=[a.id])
    issues = await svc.validate()
    assert issues == []


async def test_validate_finds_missing_dependency(svc: TodoService) -> None:
    """外部直接构造 dangling(模拟 force-delete 之后)— 这里通过外部 yaml 操作模拟。"""
    await svc.create(title="a")
    # 强制让 b 引用已删除 task(通过内部绕过 Service.create 直接写 yaml)
    import yaml
    yaml_path = svc.project_root / ".cc-harness" / "todos" / "todos.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    data["tasks"][0]["depends_on"] = ["ghost1234"]
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    issues = await svc.validate()
    assert any(i.rule_id == "missing_dependency" for i in issues)


async def test_validate_finds_cycle(svc: TodoService) -> None:
    """外部构造环:yaml 改 a.depends_on=[b],b.depends_on=[a]。"""
    a = await svc.create(title="a")
    b = await svc.create(title="b", depends_on=[a.id])
    import yaml
    yaml_path = svc.project_root / ".cc-harness" / "todos" / "todos.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    # 让 a 反向依赖 b
    for entry in data["tasks"]:
        if entry["id"] == a.id:
            entry["depends_on"] = [b.id]
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    issues = await svc.validate()
    assert any(i.rule_id == "cycle" for i in issues)


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


async def test_subscribe_fires_on_create(svc: TodoService) -> None:
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda t, e: events.append((t.id, e.kind)))
    t = await svc.create(title="x")
    assert (t.id, "created") in events


async def test_subscribe_fires_on_update(svc: TodoService) -> None:
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda t, e: events.append((t.id, e.kind)))
    t = await svc.create(title="x")
    await svc.update(t.id, title="y")
    assert (t.id, "updated") in events


async def test_subscribe_fires_on_status_changed(svc: TodoService) -> None:
    events: list[tuple[str, str, str | None]] = []
    svc.subscribe(lambda t, e: events.append((t.id, e.kind, e.prev_status)))
    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    assert (t.id, "status_changed", "pending") in events


async def test_subscribe_fires_on_delete(svc: TodoService) -> None:
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda t, e: events.append((t.id, e.kind)))
    t = await svc.create(title="x")
    await svc.delete(t.id)
    assert (t.id, "deleted") in events


async def test_unsubscribe_stops_callbacks(svc: TodoService) -> None:
    events: list[str] = []

    def cb(t: TodoTask, e: TodoEvent) -> None:
        events.append(e.kind)

    svc.subscribe(cb)
    await svc.create(title="x")
    svc.unsubscribe(cb)
    await svc.create(title="y")
    # events 仅有 1 个(x 的 created),y 的不触发
    assert events == ["created"]


async def test_unsubscribe_nonexistent_callback_is_noop(svc: TodoService) -> None:
    """unsubscribe 一个从未 subscribe 过的 callback → swallow(覆盖 ValueError 分支)。"""

    def cb(t: TodoTask, e: TodoEvent) -> None:
        return None

    # 不应抛
    svc.unsubscribe(cb)


async def test_subscribe_subscriber_exception_does_not_break_service(svc: TodoService) -> None:
    """subscriber 抛异常 → swallow,Service 不崩。"""

    def bad_cb(t: TodoTask, e: TodoEvent) -> None:
        raise RuntimeError("subscriber error")

    svc.subscribe(bad_cb)
    # Service 仍正常工作
    t = await svc.create(title="x")
    assert t.title == "x"


# ---------------------------------------------------------------------------
# _on_completion hook(用 memory_bridge + AsyncMock)
# ---------------------------------------------------------------------------


async def test_completion_hook_calls_memory_bridge(proj: Path, manifest: Manifest) -> None:
    """spec line 615:status 非 done → done 时 await memory_bridge。"""
    from dataclasses import replace

    mem = AsyncMock()
    manifest2 = replace(manifest, memory=replace(
        manifest.memory, integration=replace(
            manifest.memory.integration, completion_capture=True,
        ),
    ))
    svc = TodoService(project_root=proj, manifest=manifest2, memory_service=mem)

    t = await svc.create(title="finish me", session_id="sess-X")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")

    mem.save.assert_awaited_once()
    args, kwargs = mem.save.call_args
    text = args[0] if args else kwargs.get("text")
    assert text is not None
    assert "finish me" in text
    assert t.id in text
    assert kwargs["source"] == "todo/completion"
    assert kwargs["session_id"] == "sess-X"


async def test_completion_hook_skipped_when_completion_capture_disabled(
    proj: Path, manifest: Manifest,
) -> None:
    """manifest 默认 completion_capture=False → mem.save 不调。"""
    mem = AsyncMock()
    svc = TodoService(project_root=proj, manifest=manifest, memory_service=mem)
    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")
    mem.save.assert_not_called()


async def test_completion_hook_skipped_when_memory_service_none(
    proj: Path, manifest: Manifest,
) -> None:
    """memory_service=None → 不调(bridge 内部 swallow)。"""
    from dataclasses import replace
    manifest2 = replace(manifest, memory=replace(
        manifest.memory, integration=replace(
            manifest.memory.integration, completion_capture=True,
        ),
    ))
    svc = TodoService(project_root=proj, manifest=manifest2, memory_service=None)
    t = await svc.create(title="x")
    # 不应抛异常
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")


async def test_completion_hook_swallow_exception(
    proj: Path, manifest: Manifest,
) -> None:
    """memory_bridge 抛异常 → Service.update 不冒泡(spec line 624)。"""
    from dataclasses import replace

    mem = AsyncMock()
    mem.save.side_effect = RuntimeError("memory boom")
    manifest2 = replace(manifest, memory=replace(
        manifest.memory, integration=replace(
            manifest.memory.integration, completion_capture=True,
        ),
    ))
    svc = TodoService(project_root=proj, manifest=manifest2, memory_service=mem)

    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    # 不抛
    updated = await svc.update(t.id, status="done")
    assert updated.status == "done"


async def test_completion_hook_not_fired_when_already_done(
    proj: Path, manifest: Manifest,
) -> None:
    """spec line 615:prev_status == 'done' → 不再触发(避免重复 capture)。"""
    from dataclasses import replace

    mem = AsyncMock()
    manifest2 = replace(manifest, memory=replace(
        manifest.memory, integration=replace(
            manifest.memory.integration, completion_capture=True,
        ),
    ))
    svc = TodoService(project_root=proj, manifest=manifest2, memory_service=mem)

    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    await svc.update(t.id, status="done")
    mem.save.assert_awaited_once()

    # 即使尝试其他字段更新,mem.save 不再调用
    await svc.update(t.id, title="renamed")
    mem.save.assert_awaited_once()  # 还是 1 次


async def test_completion_hook_swallow_defense_in_depth(
    proj: Path, manifest: Manifest, monkeypatch,
) -> None:
    """bridge 自身 swallow 失败时,Service._on_completion 还有第二道兜底。

    通过 monkeypatch 让 memory_bridge.on_task_completion 抛异常,验证
    Service._on_completion 仍不冒泡(覆盖 service.py:460-461 except 分支)。
    """
    from dataclasses import replace
    import cc_harness.project.memory_bridge as mb
    import cc_harness.project.service as svc_mod

    async def explode(*args, **kwargs):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(mb, "on_task_completion", explode)
    monkeypatch.setattr(svc_mod, "on_task_completion", explode)

    mem = AsyncMock()
    manifest2 = replace(manifest, memory=replace(
        manifest.memory, integration=replace(
            manifest.memory.integration, completion_capture=True,
        ),
    ))
    svc = TodoService(project_root=proj, manifest=manifest2, memory_service=mem)

    t = await svc.create(title="x")
    await svc.update(t.id, status="in_progress")
    # 不抛
    updated = await svc.update(t.id, status="done")
    assert updated.status == "done"


# ---------------------------------------------------------------------------
# Exception 继承(防御)
# ---------------------------------------------------------------------------


async def test_task_not_found_inherits_todo_error() -> None:
    assert issubclass(TaskNotFound, TodoError)


async def test_invalid_field_inherits_todo_error() -> None:
    assert issubclass(InvalidFieldError, TodoError)


async def test_dependency_cycle_error_inherits_todo_error_via_service_path() -> None:
    """Service.update 抛 DependencyCycleError 时也是 TodoError(便于上层统一 catch)。"""
    svc_local: TodoService  # type: ignore[assignment]
    # 复用上面 fixture 创建的实例,直接造场景
    # 用 conftest-less 方式
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "p"
        proj.mkdir()
        todos = proj / ".cc-harness" / "todos"
        todos.mkdir(parents=True)
        (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
        manifest = Manifest(
            project_id="x", name="x", todos_path=".cc-harness/todos",
            created_at=datetime.now(timezone.utc),
        )
        svc_local = TodoService(project_root=proj, manifest=manifest)
        a = await svc_local.create(title="a")
        b = await svc_local.create(title="b", depends_on=[a.id])
        with pytest.raises(TodoError):
            await svc_local.update(a.id, depends_on=[b.id])