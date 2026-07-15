"""Tests for `cc-harness --resume / --resume-id / --no-resume` CLI。

cmd_resume 在 Task 6 才真正接入 REPL 自动 resume;本任务阶段它仅做
"打印什么会被 resume"的 stub — 等同于 `_select_resume_task` 的可见化。

设计(per spec line 569-580):
    _select_resume_task(tasks) — 取 in_progress 中 updated_at 最新的;
    0 个 in_progress → None;多个 → max updated_at。

CLI 行为:
    --no-resume          → print "(no resume)" / exit 0
    --resume-id <id>     → load 那个 task,print summary / exit 0
    --resume (no id)     → 调 _select_resume_task,None → 提示 / exit 0
                           非 None → print summary / exit 0
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from cc_harness.cli._shared import cli_session_id  # noqa: F401
from cc_harness.cli.init import init_noninteractive
from cc_harness.cli.resume import cmd_resume, select_resume_task
from cc_harness.cli.todo import cmd_todo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proj(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    init_noninteractive(p, name="t")
    monkeypatch.chdir(p)
    return p


def svc_args(subcommand: str, **kwargs) -> Namespace:
    """Build Namespace for cmd_todo (non-fixture factory — used both sync/async tests)."""
    defaults = {
        "create": {
            "title": None, "description": None, "depends_on": None,
            "parent": None, "assigned_to": None, "priority": None,
            "label": None, "due_date": None, "effort_estimate": None,
            "acceptance_criteria": None,
        },
        "update": {
            "task_id": None, "title": None, "description": None,
            "status": None, "depends_on": None, "parent": None,
            "assigned_to": None, "priority": None, "label": None,
            "due_date": None, "effort_estimate": None,
            "acceptance_criteria": None,
            "append_acceptance_criteria": None,
            "clear_parent_task": False, "clear_assigned_to": False,
            "clear_priority": False, "clear_due_date": False,
            "clear_effort_estimate": False,
        },
    }
    base = {"subcommand": subcommand, "json": False}
    merged = {**defaults.get(subcommand, {}), **base, **kwargs}
    return Namespace(**merged)


# ---------------------------------------------------------------------------
# select_resume_task — pure function
# ---------------------------------------------------------------------------


def test_select_resume_task_no_in_progress_returns_none(proj):
    """全是 pending → None。"""
    cmd_todo(svc_args("create", title="pending_only"), proj)
    from cc_harness.project.manifest import load_manifest
    from cc_harness.project.storage import TodoStorage
    storage = TodoStorage(proj, load_manifest(proj))
    tasks = storage.load_all()
    selected = select_resume_task(tasks)
    assert selected is None


def test_select_resume_task_picks_latest_in_progress(proj):
    """多个 in_progress → updated_at 最大者。"""
    # 建两个 task,先后标 in_progress — 后标的那个 updated_at 更新
    cmd_todo(svc_args("create", title="first"), proj)
    cmd_todo(svc_args("create", title="second"), proj)
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    first_id = data["tasks"][0]["id"]
    second_id = data["tasks"][1]["id"]
    cmd_todo(
        svc_args("update", task_id=first_id, status="in_progress"),
        proj,
    )
    cmd_todo(
        svc_args("update", task_id=second_id, status="in_progress"),
        proj,
    )

    from cc_harness.project.manifest import load_manifest
    from cc_harness.project.storage import TodoStorage
    storage = TodoStorage(proj, load_manifest(proj))
    tasks = storage.load_all()
    selected = select_resume_task(tasks)
    assert selected is not None
    assert selected.id == second_id  # 最新 updated_at


# ---------------------------------------------------------------------------
# cmd_resume — flag 组合
# ---------------------------------------------------------------------------


def test_cmd_resume_no_resume_flag(proj, capsys):
    """--no-resume → 提示 + exit 0,不试图选 task。"""
    args = Namespace(resume=True, resume_id=None, no_resume=True)
    rc = cmd_resume(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no resume" in out.lower() or "skip" in out.lower()


def test_cmd_resume_id_existing(proj, capsys):
    """--resume-id <existing> → 打印 task 摘要 + exit 0。"""
    cmd_todo(svc_args("create", title="resume_me"), proj)
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]
    args = Namespace(resume=True, resume_id=tid, no_resume=False)
    rc = cmd_resume(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert tid in out
    assert "resume_me" in out


def test_cmd_resume_id_missing(proj, capsys):
    """--resume-id <不存在的 id> → 错 + exit 1。"""
    args = Namespace(resume=True, resume_id="ghost12", no_resume=False)
    rc = cmd_resume(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TaskNotFound" in err


def test_cmd_resume_no_id_no_in_progress(proj, capsys):
    """--resume 但无 in_progress → 提示 + exit 0。"""
    cmd_todo(svc_args("create", title="just_pending"), proj)
    args = Namespace(resume=True, resume_id=None, no_resume=False)
    rc = cmd_resume(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no in_progress" in out.lower() or "nothing" in out.lower()


def test_cmd_resume_picks_latest(proj, capsys):
    """--resume(无 id)有 in_progress → 自动选最新 + 打印摘要。"""
    cmd_todo(svc_args("create", title="older"), proj)
    cmd_todo(svc_args("create", title="newer"), proj)
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    older_id = data["tasks"][0]["id"]
    newer_id = data["tasks"][1]["id"]
    cmd_todo(
        svc_args("update", task_id=older_id, status="in_progress"),
        proj,
    )
    cmd_todo(
        svc_args("update", task_id=newer_id, status="in_progress"),
        proj,
    )
    args = Namespace(resume=True, resume_id=None, no_resume=False)
    rc = cmd_resume(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert newer_id in out
    assert "newer" in out


def test_cmd_resume_no_flags_acts_as_no_resume(proj, capsys):
    """既无 --resume 也无 --no-resume → 等价 no_resume。"""
    args = Namespace(resume=False, resume_id=None, no_resume=False)
    rc = cmd_resume(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no resume" in out.lower() or "skip" in out.lower()


# ---------------------------------------------------------------------------
# Spec gap fix tests
# ---------------------------------------------------------------------------


def test_resume_missing_manifest_exits_1(tmp_path, capsys):
    """无 manifest(未 init)→ cmd_resume 退 1,stderr 含 init 提示。

    load_manifest_or_exit 走 sys.exit(1),在 cmd_resume 内被 sys.excepthook / 顶层
    重新 raise SystemExit;本测试验证整体退 1。
    """
    # 注意:tmp_path 是空的,没有 .cc-harness
    args = Namespace(resume=True, resume_id="any", no_resume=False)
    with pytest.raises(SystemExit) as ei:
        cmd_resume(args, tmp_path)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "cc-harness init" in err or "No .cc-harness" in err


def test_resume_via_todo_service_not_storage(proj, capsys, monkeypatch):
    """cmd_resume 走 TodoService.list / get,不再直接用 TodoStorage。"""
    from cc_harness.cli import resume as resume_mod
    from cc_harness.project.service import TodoService

    # 记录实际被调用的方法
    list_called = []
    get_called = []

    real_list = TodoService.list
    real_get = TodoService.get

    async def spy_list(self, **kw):
        list_called.append(kw)
        return await real_list(self, **kw)

    async def spy_get(self, task_id):
        get_called.append(task_id)
        return await real_get(self, task_id)

    monkeypatch.setattr(TodoService, "list", spy_list)
    monkeypatch.setattr(TodoService, "get", spy_get)

    cmd_todo(svc_args("create", title="via_svc"), proj)
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]

    args = Namespace(resume=True, resume_id=tid, no_resume=False)
    rc = cmd_resume(args, proj)
    assert rc == 0
    # verify TodoService.get was used (not TodoStorage)
    assert tid in get_called
    # resume.py module should not import TodoStorage anymore
    assert not hasattr(resume_mod, "TodoStorage") or "TodoStorage" not in dir(resume_mod)
    # also verify list path: --resume (no id)
    args2 = Namespace(resume=True, resume_id=None, no_resume=False)
    rc2 = cmd_resume(args2, proj)
    assert rc2 == 0
    # list should have been called at least once
    assert len(list_called) >= 1


def test_resume_id_via_service_not_storage(proj, capsys, monkeypatch):
    """--resume-id 路径也走 TodoService.get(非 TodoStorage.load_all 后过滤)。"""
    from cc_harness.project.service import TodoService

    cmd_todo(svc_args("create", title="get_via_svc"), proj)
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]

    get_called = []
    real_get = TodoService.get

    async def spy_get(self, task_id):
        get_called.append(task_id)
        return await real_get(self, task_id)

    monkeypatch.setattr(TodoService, "get", spy_get)
    args = Namespace(resume=True, resume_id=tid, no_resume=False)
    rc = cmd_resume(args, proj)
    assert rc == 0
    assert tid in get_called


def test_resume_todo_error_exits_1(proj, capsys, monkeypatch):
    """TodoError 业务错(非 TaskNotFound)也走 exit 1。"""
    from cc_harness.cli import resume as resume_mod
    from cc_harness.project.exceptions import InvalidFieldError

    def boom_get(cwd, task_id):
        raise InvalidFieldError("test error")

    monkeypatch.setattr(resume_mod, "_get_via_service", boom_get)
    args = Namespace(resume=True, resume_id="any", no_resume=False)
    rc = cmd_resume(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "InvalidFieldError" in err
    assert "test error" in err
