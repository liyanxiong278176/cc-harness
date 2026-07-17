"""Sub-project C: children_all_done 纯函数测试。"""
from cc_harness.project.dependency import children_all_done
from cc_harness.project.models import TodoTask
from datetime import datetime, timezone

def _task(tid, status="pending", parent=None):
    now = datetime.now(timezone.utc)
    return TodoTask(id=tid, title=tid, status=status, description="",
                    depends_on=[], parent_task=parent, assigned_to=None,
                    priority=None, labels=[], due_date=None,
                    effort_estimate=None, acceptance_criteria=[],
                    created_at=now, updated_at=now, active_sessions=[])


def test_children_all_done_no_children():
    tasks = {"P": _task("P")}
    assert children_all_done(tasks, "P") == (True, [])


def test_children_all_done_all_done():
    tasks = {"P": _task("P"), "C1": _task("C1", "done", "P"),
             "C2": _task("C2", "done", "P")}
    assert children_all_done(tasks, "P") == (True, [])


def test_children_all_done_partial():
    tasks = {"P": _task("P"), "C1": _task("C1", "done", "P"),
             "C2": _task("C2", "pending", "P"), "C3": _task("C3", "in_progress", "P")}
    done, pending = children_all_done(tasks, "P")
    assert done is False
    assert pending == ["C2", "C3"]  # 字典序


def test_children_all_done_ignores_parent_none_roots():
    # parent_task=None 的根任务不算任何 parent 的 children,
    # 即便它们 pending / in_progress 也不阻塞 P 标 done(Task 3 完成门依赖此不变量)。
    tasks = {
        "P": _task("P"),
        "C1": _task("C1", "done", "P"),
        "R1": _task("R1", "pending", None),
        "R2": _task("R2", "in_progress", None),
    }
    assert children_all_done(tasks, "P") == (True, [])


def test_children_all_done_deterministic_order():
    tasks = {"P": _task("P")}
    for tid in ["Z", "A", "M"]:
        tasks[tid] = _task(tid, "pending", "P")
    _, pending = children_all_done(tasks, "P")
    assert pending == ["A", "M", "Z"]


def test_children_all_done_parent_missing():
    assert children_all_done({}, "ghost") == (True, [])
