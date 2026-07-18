"""C Task 2: deps 注入 last_turn_text + 8 handler 签名兼容。"""
import pytest
from cc_harness.cli.init import init_noninteractive
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService
from cc_harness.project import tools


def _make_service(tmp_path):
    manifest = init_noninteractive(tmp_path, name="c-deps", write_gitignore=False)
    return TodoService(project_root=tmp_path, manifest=manifest)


@pytest.mark.asyncio
async def test_inject_todo_tools_passes_last_turn_text(tmp_path):
    svc = _make_service(tmp_path)
    extras = inject_todo_tools(svc, "s", cwd=".", last_turn_text="hello")
    # D1 Task 5:9 个 entry(8 原 todo + dispatch_subagent)
    assert len(extras) == 9
    for e in extras:
        assert e["deps"]["last_turn_text"] == "hello"


@pytest.mark.asyncio
async def test_all_8_handlers_accept_last_turn_text_kwarg(tmp_path):
    """8 handler 全部能收 last_turn_text kwarg 不 TypeError(dispatch splat 模拟)。

    覆盖全 8 个:list/get/create/update/delete/resolve/validate/toposort。
    """
    svc = _make_service(tmp_path)
    t = await svc.create(title="x", session_id="s")
    # 每个 handler 一个最小合法 args(不触发完成门,只验证签名不 TypeError)
    cases = [
        (tools.todo_list_handler, {}),
        (tools.todo_get_handler, {"task_id": t.id}),
        (tools.todo_create_handler, {"title": "y"}),
        (tools.todo_update_handler, {"task_id": t.id, "description": "z"}),
        (tools.todo_resolve_handler, {"task_id": t.id}),
        (tools.todo_validate_handler, {}),
        (tools.todo_toposort_handler, {}),
        # delete 放最后:删后 t 不存在,get/update/resolve 需在 t 存在时跑
        (tools.todo_delete_handler, {"task_id": t.id, "force": True}),
    ]
    for handler, args in cases:
        r = await handler(args, cwd=".", service=svc, session_id="s",
                          last_turn_text="x")
        assert r is not None  # 不 TypeError 即过
