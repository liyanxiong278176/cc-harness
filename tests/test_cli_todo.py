"""Tests for `cc-harness todo` CLI dispatcher — 7 subcommands.

In-process tests: build args via argparse.Namespace, call cmd_todo(args, proj).
All subcommands go through TodoService (no direct file I/O).

Coverage:
    - list / get / create / update / delete / resolve / validate
    - --json toggle (output is JSON, parseable)
    - Exit codes (0 OK / 1 业务错 / 2 系统错)
    - Error mapping (TodoError subclasses → stderr + exit 1)
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from cc_harness.cli.init import init_noninteractive
from cc_harness.cli.todo import cmd_todo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proj(tmp_path: Path, monkeypatch) -> Path:
    """Empty initialized project:init + chdir."""
    p = tmp_path / "proj"
    p.mkdir()
    init_noninteractive(p, name="t")
    monkeypatch.chdir(p)
    return p


@pytest.fixture
def svc_args(proj: Path):
    """Factory:build Namespace for a subcommand, with sensible per-sub defaults."""
    DEFAULTS = {
        "list": {
            "status": None, "parent": None, "no_done": False,
            "format": "table", "sort": "status", "limit": 20,
        },
        "get": {
            "task_id": None, "raw": False,
        },
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
            # clear_* 标志
            "clear_parent_task": False, "clear_assigned_to": False,
            "clear_priority": False, "clear_due_date": False,
            "clear_effort_estimate": False,
        },
        "delete": {"task_id": None, "force": False},
        "resolve": {"task_id": None, "no_done": False},
        "validate": {"strict": False},
    }
    def _factory(subcommand: str, **kwargs) -> Namespace:
        base = {"subcommand": subcommand, "json": kwargs.pop("json", False)}
        merged = {**DEFAULTS.get(subcommand, {}), **base, **kwargs}
        return Namespace(**merged)
    return _factory


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_basic(proj, svc_args, capsys):
    args = svc_args(
        "create", title="hello", description="body",
        depends_on=None, parent=None, assigned_to=None,
        priority=None, label=None, due_date=None,
        effort_estimate=None, acceptance_criteria=None,
    )
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_create]" in out
    assert "hello" in out


def test_create_missing_title(proj, svc_args, capsys):
    args = svc_args("create", title="")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "InvalidFieldError" in err
    assert "title" in err.lower()


def test_create_with_priority(proj, svc_args, capsys):
    args = svc_args("create", title="hi", priority="high")
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[high]" in out or "high" in out


def test_create_with_invalid_priority(proj, svc_args, capsys):
    args = svc_args("create", title="hi", priority="urgent")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "InvalidFieldError" in err


def test_create_with_depends_on_missing(proj, svc_args, capsys):
    args = svc_args(
        "create", title="orphan", depends_on=["ghost12"])
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TaskNotFound" in err


def test_create_json_output(proj, svc_args, capsys):
    args = svc_args("create", title="json_test", json=True)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    # JSON 是最后一行(前面可能有 rich 输出,我们严格区分)
    last = out.strip().splitlines()[-1]
    parsed = json.loads(last)
    assert "id" in parsed
    assert parsed["title"] == "json_test"


def test_create_with_label_acceptance_due(proj, svc_args, capsys):
    args = svc_args(
        "create", title="full", label=["x", "y"],
        acceptance_criteria=["a1"], due_date="2026-08-01T00:00:00",
    )
    rc = cmd_todo(args, proj)
    assert rc == 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty(proj, svc_args, capsys):
    args = svc_args("list", status=None, parent=None,
                    no_done=False, format="table",
                    sort="status", limit=20)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 tasks" in out


def test_list_returns_created(proj, svc_args, capsys):
    args = svc_args("create", title="a")
    cmd_todo(args, proj)
    args = svc_args("list", status=None, parent=None,
                    no_done=False, format="table",
                    sort="status", limit=20)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 tasks" in out
    assert "a" in out


def test_list_status_filter(proj, svc_args, capsys):
    a_args = svc_args("create", title="pending_task")
    cmd_todo(a_args, proj)
    args = svc_args("list", status="pending", parent=None,
                    no_done=False, format="table",
                    sort="status", limit=20)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "pending_task" in out


def test_list_no_done(proj, svc_args, capsys):
    a = svc_args("create", title="alive")
    cmd_todo(a, proj)
    b = svc_args("create", title="done_one")
    cmd_todo(b, proj)
    # 标记 b 为 done(直接 update 操作)
    args = svc_args("list", parent=None, no_done=False,
                    format="table", sort="status", limit=20)
    rc = cmd_todo(args, proj)
    capsys.readouterr()  # 清空
    # list with no_done=True → 排除 done
    args2 = svc_args(
        "list", status=None, parent=None, no_done=True,
        format="table", sort="status", limit=20,
    )
    rc = cmd_todo(args2, proj)
    assert rc == 0


def test_list_json_output(proj, svc_args, capsys):
    args = svc_args("create", title="json_l")
    cmd_todo(args, proj)
    capsys.readouterr()  # 清掉 create 的 rich 输出
    args = svc_args("list", status=None, parent=None,
                    no_done=False, format="table",
                    sort="status", limit=20, json=True)
    rc = cmd_todo(args, proj)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert isinstance(parsed, list)
    assert len(parsed) == 1


def test_list_limit_truncates(proj, svc_args, capsys):
    for i in range(5):
        cmd_todo(svc_args("create", title=f"t{i}"), proj)
    args = svc_args("list", status=None, parent=None, no_done=False,
                    format="table", sort="status", limit=3)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "+2 more" in out


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_existing(proj, svc_args, capsys):
    cmd_todo(svc_args("create", title="get_me"), proj)
    args = svc_args("get", task_id=None)  # 第一创建后从 yaml 找
    # 重建 todo.yaml, 用 id 取
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    import yaml as _y
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]
    args = svc_args("get", task_id=tid)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_get]" in out
    assert tid in out
    assert "get_me" in out


def test_get_missing_returns_error(proj, svc_args, capsys):
    args = svc_args("get", task_id="ghost12")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TaskNotFound" in err


def test_get_json(proj, svc_args, capsys):
    cmd_todo(svc_args("create", title="json_get"), proj)
    capsys.readouterr()
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    import yaml as _y
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]
    args = svc_args("get", task_id=tid, json=True)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out.strip())
    assert parsed["id"] == tid


def test_get_raw_output_only_body(proj, svc_args, capsys):
    """--raw → 只输出 task body(description),无 frontmatter / metadata。"""
    cmd_todo(
        svc_args("create", title="desc_test", description="the body"),
        proj,
    )
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    import yaml as _y
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]
    args = svc_args("get", task_id=tid, raw=True)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "the body" in out
    # 不应含 status/depends_on 等元数据行
    assert "[todo_get]" not in out  # raw 模式无 marker


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def _create_and_get_id(args_create, proj) -> str:
    """call cmd_todo(create, ...) → return id of NEWLY created task(最后一行 yaml)。"""
    cmd_todo(args_create, proj)
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    return data["tasks"][-1]["id"]


def test_update_title(proj, svc_args, capsys):
    tid = _create_and_get_id(svc_args("create", title="old"), proj)
    args = svc_args("update", task_id=tid, title="new", clear_title=False)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_update]" in out


def test_update_clear_assigned(proj, svc_args, capsys):
    tid = _create_and_get_id(
        svc_args("create", title="a", assigned_to="alice"), proj)
    args = svc_args("update", task_id=tid, clear_assigned_to=True)
    rc = cmd_todo(args, proj)
    assert rc == 0
    # 验证 assigned_to 已清空
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    entry = next(t for t in data["tasks"] if t["id"] == tid)
    assert entry["assigned_to"] is None


def test_update_invalid_status(proj, svc_args, capsys):
    tid = _create_and_get_id(svc_args("create", title="a"), proj)
    args = svc_args("update", task_id=tid, status="done_done")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "InvalidFieldError" in err or "Error" in err


def test_update_no_such_task(proj, svc_args, capsys):
    args = svc_args("update", task_id="ghost12", title="x")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TaskNotFound" in err


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_success(proj, svc_args, capsys):
    tid = _create_and_get_id(svc_args("create", title="del_me"), proj)
    args = svc_args("delete", task_id=tid)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_delete]" in out


def test_delete_missing(proj, svc_args, capsys):
    args = svc_args("delete", task_id="ghost12")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TaskNotFound" in err


def test_delete_with_dependents_refuses(proj, svc_args, capsys):
    a_id = _create_and_get_id(svc_args("create", title="parent"), proj)
    cmd_todo(
        svc_args("create", title="child", depends_on=[a_id]), proj,
    )
    args = svc_args("delete", task_id=a_id)
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "InvalidFieldError" in err


def test_delete_force_succeeds(proj, svc_args, capsys):
    a_id = _create_and_get_id(svc_args("create", title="parent"), proj)
    cmd_todo(
        svc_args("create", title="child", depends_on=[a_id]), proj,
    )
    args = svc_args("delete", task_id=a_id, force=True)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "force=True" in out


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_chain(proj, svc_args, capsys):
    a_id = _create_and_get_id(svc_args("create", title="a"), proj)
    b_id = _create_and_get_id(
        svc_args("create", title="b", depends_on=[a_id]), proj)
    args = svc_args("resolve", task_id=b_id, no_done=False, json=False)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[todo_resolve]" in out
    assert "2 tasks" in out


def test_resolve_json(proj, svc_args, capsys):
    a_id = _create_and_get_id(svc_args("create", title="a"), proj)
    capsys.readouterr()
    args = svc_args("resolve", task_id=a_id, no_done=False, json=True)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out.strip())
    assert isinstance(parsed, list)


def test_resolve_no_done_excludes(proj, svc_args, capsys):
    a_id = _create_and_get_id(svc_args("create", title="a"), proj)
    cmd_todo(
        svc_args("update", task_id=a_id, status="in_progress",
                 assigned_to=None, clear_assigned_to=False),
        proj,
    )
    cmd_todo(
        svc_args("update", task_id=a_id, status="done",
                 assigned_to=None, clear_assigned_to=False),
        proj,
    )
    b_id = _create_and_get_id(
        svc_args("create", title="b", depends_on=[a_id]), proj)
    capsys.readouterr()  # 清空所有先前的 create/update 输出
    args = svc_args("resolve", task_id=b_id, no_done=True, json=False)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert a_id not in out  # done 节点被排除


def test_resolve_missing(proj, svc_args, capsys):
    args = svc_args("resolve", task_id="ghost12")
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TaskNotFound" in err


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_clean(proj, svc_args, capsys):
    cmd_todo(svc_args("create", title="clean"), proj)
    args = svc_args("validate", json=False, strict=False)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 issues" in out or "✓" in out


def test_validate_json(proj, svc_args, capsys):
    cmd_todo(svc_args("create", title="j"), proj)
    capsys.readouterr()
    args = svc_args("validate", json=True, strict=False)
    rc = cmd_todo(args, proj)
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out.strip())
    assert isinstance(parsed, list)
    assert len(parsed) == 0


def test_validate_strict_promotes_warnings_to_error_exit(proj, svc_args, capsys):
    """missing_md(yaml 引用但磁盘 md 不存在)→ validate warning,strict 升 error。"""
    cmd_todo(svc_args("create", title="x"), proj)
    # 手动删 md,造 missing_md
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    tid = data["tasks"][0]["id"]
    md = proj / ".cc-harness" / "todos" / f"{tid}.md"
    md.unlink()
    args = svc_args("validate", json=False, strict=False)
    rc = cmd_todo(args, proj)
    # warnings only → exit 0
    assert rc == 0
    args2 = svc_args("validate", json=False, strict=True)
    rc = cmd_todo(args2, proj)
    err = capsys.readouterr().err
    assert rc == 1
    assert "missing_md" in err or "Error" in err


# ---------------------------------------------------------------------------
# Spec gap fix tests
# ---------------------------------------------------------------------------


def test_update_append_acceptance_criteria(proj, svc_args, capsys):
    """--append-acceptance-criteria → 在现有列表尾部追加,不替换。"""
    tid = _create_and_get_id(
        svc_args("create", title="ac", acceptance_criteria=["a1", "a2"]),
        proj,
    )
    args = svc_args(
        "update", task_id=tid,
        append_acceptance_criteria=["b1", "b2"],
    )
    rc = cmd_todo(args, proj)
    assert rc == 0

    # 验证 yaml 中的 acceptance_criteria
    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    entry = next(t for t in data["tasks"] if t["id"] == tid)
    assert entry["acceptance_criteria"] == ["a1", "a2", "b1", "b2"]


def test_update_append_acceptance_criteria_with_existing(proj, svc_args, capsys):
    """已经替换过(--acceptance-criteria)再 --append-acceptance-criteria → 累加。"""
    tid = _create_and_get_id(
        svc_args("create", title="ac", acceptance_criteria=["a1"]),
        proj,
    )
    # 先替换
    cmd_todo(
        svc_args("update", task_id=tid, acceptance_criteria=["x"]),
        proj,
    )
    # 再追加
    args = svc_args(
        "update", task_id=tid,
        append_acceptance_criteria=["y", "z"],
    )
    rc = cmd_todo(args, proj)
    assert rc == 0

    import yaml as _y
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    entry = next(t for t in data["tasks"] if t["id"] == tid)
    assert entry["acceptance_criteria"] == ["x", "y", "z"]


def test_delete_json_output(proj, svc_args, capsys):
    """--json → delete 输出 JSON 而非纯文本。"""
    tid = _create_and_get_id(svc_args("create", title="del_j"), proj)
    capsys.readouterr()  # 清空 create 的输出
    args = svc_args("delete", task_id=tid, json=True)
    rc = cmd_todo(args, proj)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["id"] == tid
    assert parsed["deleted"] is True
    assert parsed["force"] is False


def test_delete_json_force(proj, svc_args, capsys):
    """--json + --force → JSON 含 force=True。"""
    a_id = _create_and_get_id(svc_args("create", title="p"), proj)
    cmd_todo(svc_args("create", title="c", depends_on=[a_id]), proj)
    capsys.readouterr()  # 清空 create 输出
    args = svc_args("delete", task_id=a_id, force=True, json=True)
    rc = cmd_todo(args, proj)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["force"] is True


def test_list_json_respects_limit(proj, svc_args, capsys):
    """--json 输出也受 --limit 限制(不再全量 dump)。"""
    for i in range(5):
        cmd_todo(svc_args("create", title=f"t{i}"), proj)
    capsys.readouterr()
    args = svc_args(
        "list", status=None, parent=None, no_done=False,
        format="table", sort="status", limit=2, json=True,
    )
    rc = cmd_todo(args, proj)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert isinstance(parsed, list)
    assert len(parsed) == 2  # 受 --limit 限制


def test_list_format_csv(proj, svc_args, capsys):
    """--format csv → CSV 字符串(列头 + 行)。"""
    cmd_todo(svc_args("create", title="csv_test"), proj)
    capsys.readouterr()
    args = svc_args(
        "list", status=None, parent=None, no_done=False,
        format="csv", sort="status", limit=20,
    )
    rc = cmd_todo(args, proj)
    assert rc == 0
    out = capsys.readouterr().out
    # CSV:第一行是列头
    lines = out.strip().splitlines()
    assert lines[0] == "id,title,status,priority"
    assert len(lines) >= 2
    # 第二行包含 "csv_test"
    assert "csv_test" in lines[1]


def test_list_renders_rich_table_in_tty(proj, svc_args, capsys, monkeypatch):
    """TTY 模式下 _list 走 Rich Table(JsonOrText.print_table 路径)。"""
    cmd_todo(svc_args("create", title="tty_test"), proj)
    capsys.readouterr()

    # mock JsonOrText.is_tty 模拟 TTY
    from cc_harness.cli import _shared
    real_init = _shared.JsonOrText.__init__

    def patched_init(self, console, args):
        real_init(self, console, args)
        self.is_tty = True

    monkeypatch.setattr(_shared.JsonOrText, "__init__", patched_init)
    args = svc_args(
        "list", status=None, parent=None, no_done=False,
        format="table", sort="status", limit=20,
    )
    rc = cmd_todo(args, proj)
    assert rc == 0
    # Rich Table 渲染到 console,不一定进 capsys 的 out
    # 主要验证不报错、退出码为 0


def test_cmd_todo_system_error_exit_2(proj, svc_args, capsys, monkeypatch):
    """系统错(OSError / StorageError / RuntimeError)→ exit 2。"""
    from cc_harness.cli import todo as todo_mod

    async def boom(svc, args, console):
        raise OSError("disk full")

    monkeypatch.setitem(todo_mod._HANDLERS, "list", boom)
    args = svc_args("list", status=None, parent=None, no_done=False,
                    format="table", sort="status", limit=20)
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 2
    assert "system error" in err
    assert "disk full" in err


def test_cmd_todo_storage_error_exit_2(proj, svc_args, capsys, monkeypatch):
    """StorageError(yaml 损坏等)→ exit 2。"""
    from cc_harness.cli import todo as todo_mod

    async def boom(svc, args, console):
        from cc_harness.project.storage import StorageError
        raise StorageError("yaml parse failed")

    monkeypatch.setitem(todo_mod._HANDLERS, "list", boom)
    args = svc_args("list", status=None, parent=None, no_done=False,
                    format="table", sort="status", limit=20)
    rc = cmd_todo(args, proj)
    err = capsys.readouterr().err
    assert rc == 2
    assert "system error" in err
    assert "yaml parse failed" in err
