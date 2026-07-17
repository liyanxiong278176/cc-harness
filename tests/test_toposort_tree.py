"""C Task 4: todo_toposort view=tree HTN 缩进树渲染。"""
import pytest
from cc_harness.project.tools import _render_toposort, todo_toposort_handler
from cc_harness.project.models import TodoTask
from datetime import datetime, timezone

def _task(tid, status="pending", parent=None):
    now = datetime.now(timezone.utc)
    return TodoTask(id=tid, title=f"title-{tid}", status=status, description="",
                    depends_on=[], parent_task=parent, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    created_at=now, updated_at=now, active_sessions=[])
def _indent_of(line):
    """行前导空白数。"""
    return len(line) - len(line.lstrip())


def test_render_tree_single_level_indent():
    by_id = {"P": _task("P", "in_progress"),
             "C1": _task("C1", "done", "P"), "C2": _task("C2", "pending", "P")}
    out = _render_toposort(["P", "C1", "C2"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "HTN 树视图" in out
    p_line = next(line for line in out.splitlines() if line.lstrip().startswith("P"))
    c_line = next(line for line in out.splitlines() if line.lstrip().startswith("C1"))
    assert _indent_of(c_line) > _indent_of(p_line)  # child 比 parent 缩进深


def test_render_tree_nested_grandchildren():
    by_id = {"P": _task("P"), "C": _task("C", parent="P"),
             "G": _task("G", parent="C")}
    out = _render_toposort(["P", "C", "G"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    p = next(line for line in out.splitlines() if line.lstrip().startswith("P"))
    c = next(line for line in out.splitlines() if line.lstrip().startswith("C"))
    g = next(line for line in out.splitlines() if line.lstrip().startswith("G"))
    assert _indent_of(g) > _indent_of(c) > _indent_of(p)  # 三层递增


def test_render_tree_cycle_visited_safeguard():
    by_id = {"P": _task("P", parent="C"), "C": _task("C", parent="P")}
    out = _render_toposort(["P", "C"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "cycle" in out.lower() or "⚠" in out  # 标环,不崩(不无限递归)


def test_render_tree_mixed_top_level_and_children():
    by_id = {"P": _task("P"), "C": _task("C", parent="P"), "T": _task("T")}
    out = _render_toposort(["P", "C", "T"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "T" in out  # 另一顶层也显示


def test_render_tree_dangling_parent_orphan_annotated():
    """parent 已删(不在 by_id)的 child → 升为顶层 + orphan 标注(dangling_parent)。"""
    by_id = {"C": _task("C", parent="P_deleted")}  # P_deleted 不在 by_id
    out = _render_toposort(["C"], list(by_id.values()), by_id,
                           None, group="all", view="tree")
    assert "HTN 树视图" in out
    c_line = next(line for line in out.splitlines() if line.lstrip().startswith("C"))
    assert "(orphan" in c_line  # dangling_parent 标 orphan 注释(M6:锁 orphan code path)


def test_render_flat_default_unchanged():
    """view=flat(默认)回归:跟 B 现状一致。"""
    by_id = {"P": _task("P"), "C": _task("C", parent="P")}
    out_flat = _render_toposort(["P", "C"], list(by_id.values()), by_id,
                                None, group="all", view="flat")
    out_default = _render_toposort(["P", "C"], list(by_id.values()), by_id,
                                   None, group="all")
    assert out_flat == out_default
    assert "Topo order" in out_flat  # flat 标志段


@pytest.mark.asyncio
async def test_toposort_handler_view_tree(tmp_path):
    from cc_harness.cli.init import init_noninteractive
    from cc_harness.project.service import TodoService
    manifest = init_noninteractive(tmp_path, name="c-tree", write_gitignore=False)
    svc = TodoService(project_root=tmp_path, manifest=manifest)
    p = await svc.create(title="parent", session_id="s")
    await svc.create(title="child", parent_task=p.id, session_id="s")
    r = await todo_toposort_handler({"view": "tree"}, service=svc,
                                    session_id="s", cwd=".", last_turn_text="")
    assert r.is_error is False
    assert "HTN 树视图" in r.llm_text
