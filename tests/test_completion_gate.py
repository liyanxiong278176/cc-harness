"""C Task 3: todo_update 完成门(聚合 + acceptance + force)。"""
import pytest
from cc_harness.cli.init import init_noninteractive
from cc_harness.project.service import TodoService
from cc_harness.project.tools import todo_update_handler

def _make_service(tmp_path):
    manifest = init_noninteractive(tmp_path, name="c-gate", write_gitignore=False)
    return TodoService(project_root=tmp_path, manifest=manifest)

async def _create(svc, title, status="pending", criteria=None, deps=None,
                  parent=None, session_id="s"):
    t = await svc.create(title=title, acceptance_criteria=criteria or [],
                         depends_on=deps or [], parent_task=parent,
                         session_id=session_id)
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t

async def _update_done(svc, task_id, force=False, last_turn_text=""):
    args = {"task_id": task_id, "status": "done"}
    if force:
        args["force"] = True
    return await todo_update_handler(args, service=svc, session_id="s",
                                     cwd=".", last_turn_text=last_turn_text)


@pytest.mark.asyncio
async def test_gate_blocks_when_children_pending(tmp_path):
    svc = _make_service(tmp_path)
    p = await _create(svc, "parent", status="in_progress")
    c = await _create(svc, "child", parent=p.id)  # pending child
    r = await _update_done(svc, p.id)
    assert r.is_error is True
    assert c.id in r.llm_text and "子任务" in r.llm_text


@pytest.mark.asyncio
async def test_gate_blocks_acceptance_not_met(tmp_path):
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["必须包含 unit test"])
    r = await _update_done(svc, t.id, last_turn_text="我改了代码")
    assert r.is_error is True
    assert "acceptance" in r.llm_text
    assert "可用 force=true 绕过" in r.llm_text  # acceptance-only hint 文案回归
    assert (await svc.get(t.id)).status == "in_progress"  # 没真 update


@pytest.mark.asyncio
async def test_gate_both_errors_reported(tmp_path):
    svc = _make_service(tmp_path)
    p = await _create(svc, "p", status="in_progress", criteria=["要 AC1"])
    await _create(svc, "c", parent=p.id)
    r = await _update_done(svc, p.id, last_turn_text="nope")
    assert r.is_error is True
    assert "子任务" in r.llm_text
    assert "acceptance" in r.llm_text
    assert "子任务聚合不可绕" in r.llm_text  # children+acceptance hint 文案回归


@pytest.mark.asyncio
async def test_gate_force_bypasses_acceptance(tmp_path):
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    r = await _update_done(svc, t.id, force=True, last_turn_text="nope")
    assert r.is_error is False
    assert (await svc.get(t.id)).status == "done"


@pytest.mark.asyncio
async def test_gate_force_does_not_bypass_aggregation(tmp_path):
    svc = _make_service(tmp_path)
    p = await _create(svc, "p", status="in_progress")
    await _create(svc, "c", parent=p.id)
    r = await _update_done(svc, p.id, force=True)
    assert r.is_error is True and "子任务" in r.llm_text


@pytest.mark.asyncio
async def test_gate_empty_criteria_skips_acceptance(tmp_path):
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress")
    r = await _update_done(svc, t.id)
    assert r.is_error is False


@pytest.mark.asyncio
async def test_gate_passes_when_all_good(tmp_path):
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    r = await _update_done(svc, t.id, last_turn_text="我写了 AC1 的 unit test")
    assert r.is_error is False
    assert (await svc.get(t.id)).status == "done"


@pytest.mark.asyncio
async def test_gate_not_triggered_for_non_done_update(tmp_path):
    """改 title 等非 status=done 的 update 完全不触发 gate(回归保护)。"""
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    r = await todo_update_handler({"task_id": t.id, "title": "new title"},
                                  service=svc, session_id="s", cwd=".",
                                  last_turn_text="")
    assert r.is_error is False
    assert (await svc.get(t.id)).title == "new title"


@pytest.mark.asyncio
async def test_gate_idempotent_already_done(tmp_path):
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["要 AC1"])
    await _update_done(svc, t.id, force=True)  # 先标 done
    r = await _update_done(svc, t.id, last_turn_text="nope")  # 再设 done
    assert r.is_error is False  # 已 done,放行


@pytest.mark.asyncio
async def test_gate_failsoft_on_service_list_error(tmp_path, monkeypatch):
    """service.list 抛 → fail-soft 放行。"""
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["AC1"])
    async def boom(*a, **k): raise RuntimeError("boom")
    monkeypatch.setattr(svc, "list", boom)
    r = await _update_done(svc, t.id, last_turn_text="nope")
    assert r.is_error is False  # fail-soft 放行


@pytest.mark.asyncio
async def test_gate_failsoft_on_run_verify_error(tmp_path, monkeypatch):
    """run_verify 抛 → fail-soft 跳过 acceptance,聚合仍跑(spec 错误表行)。"""
    svc = _make_service(tmp_path)
    t = await _create(svc, "t", status="in_progress", criteria=["AC1"])
    import cc_harness.project.tools as tools_mod
    # run_verify 是同步函数,monkeypatch 同步版
    def boom_sync(*a, **k): raise RuntimeError("verify boom")
    monkeypatch.setattr(tools_mod, "run_verify", boom_sync)
    r = await _update_done(svc, t.id, last_turn_text="nope")
    # acceptance fail-soft 跳过 → 无 children → 放行
    assert r.is_error is False
