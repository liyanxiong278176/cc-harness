"""TodoTask / ValidationIssue / TodoEvent / RuleId / Manifest 单元测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cc_harness.project.models import (
    LiveConfig,
    Manifest,
    MemoryConfig,
    MemoryIntegrationConfig,
    RuleId,
    TodoEvent,
    TodoTask,
    ValidationIssue,
    _parse_iso,
)


# ---------------------------------------------------------------------------
# TodoTask 字段
# ---------------------------------------------------------------------------


def test_todo_task_required_fields_minimal():
    """最小可构造 TodoTask(全部 Optional 用 None/默认值)。"""
    now = datetime.now(timezone.utc)
    t = TodoTask(
        id="abc12345",
        title="hello",
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
    assert t.id == "abc12345"
    assert t.status == "pending"
    assert t.active_sessions == []  # 系统字段默认空


def test_todo_task_full_fields():
    """填满 T15 字段。"""
    now = datetime.now(timezone.utc)
    due = datetime(2026, 8, 1, tzinfo=timezone.utc)
    t = TodoTask(
        id="def67890",
        title="full",
        status="in_progress",
        description="markdown body",
        depends_on=["abc12345"],
        parent_task="root-1",
        assigned_to="user",
        priority="high",
        labels=["backend", "p1"],
        due_date=due,
        effort_estimate=4.5,
        acceptance_criteria=["AC1", "AC2"],
        created_at=now,
        updated_at=now,
        active_sessions=["sess-A", "sess-B"],
    )
    assert t.depends_on == ["abc12345"]
    assert t.priority == "high"
    assert t.effort_estimate == 4.5
    assert t.acceptance_criteria == ["AC1", "AC2"]
    assert t.active_sessions == ["sess-A", "sess-B"]


def test_todo_task_field_count():
    """T15 = 11 user + 3 auto + 1 system,另有 1 个不持久化的内部辅助字段。

    持久化字段:
      user-controllable(11): title / status / description / depends_on / parent_task /
                             assigned_to / priority / labels / due_date / effort_estimate /
                             acceptance_criteria
      auto-gen(3):           id / created_at / updated_at
      system(1):             active_sessions
    内部辅助(1):             truncated_note
    """
    fields = {f for f in TodoTask.__dataclass_fields__.keys()}
    expected = {
        # T11
        "title", "status", "description", "depends_on", "parent_task",
        "assigned_to", "priority", "labels", "due_date", "effort_estimate",
        "acceptance_criteria",
        # T14
        "id", "created_at", "updated_at",
        # T15
        "active_sessions",
        # 内部辅助(不持久化)
        "truncated_note",
    }
    assert fields == expected
    assert len(fields) == 16


def test_todo_task_yaml_dict_roundtrip():
    """to_yaml_dict + from_yaml_dict 互逆(忽略 None vs 缺失差异)。"""
    now = datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc)
    due = datetime(2026, 8, 1, tzinfo=timezone.utc)
    t = TodoTask(
        id="xyz11111",
        title="rt",
        status="done",
        description="body",
        depends_on=["a", "b"],
        parent_task="p",
        assigned_to="user",
        priority="critical",
        labels=["x"],
        due_date=due,
        effort_estimate=1.0,
        acceptance_criteria=["AC"],
        created_at=now,
        updated_at=now,
        active_sessions=["s"],
    )
    d = t.to_yaml_dict()
    assert "truncated_note" not in d
    t2 = TodoTask.from_yaml_dict(d)
    assert t2.id == t.id
    assert t2.title == t.title
    assert t2.status == t.status
    assert t2.description == t.description
    assert t2.depends_on == t.depends_on
    assert t2.parent_task == t.parent_task
    assert t2.priority == t.priority
    assert t2.labels == t.labels
    assert t2.due_date == t.due_date
    assert t2.effort_estimate == t.effort_estimate
    assert t2.acceptance_criteria == t.acceptance_criteria
    assert t2.active_sessions == t.active_sessions


def test_todo_task_from_yaml_dict_missing_optional_fields():
    """yaml 中缺可选字段 → 用默认值兜底。"""
    now = datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc)
    minimal = {
        "id": "min00001",
        "title": "min",
        "status": "pending",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    t = TodoTask.from_yaml_dict(minimal)
    assert t.description == ""
    assert t.depends_on == []
    assert t.parent_task is None
    assert t.priority is None
    assert t.labels == []
    assert t.due_date is None
    assert t.effort_estimate is None
    assert t.acceptance_criteria == []
    assert t.active_sessions == []


def test_parse_iso_z_suffix():
    """_parse_iso 容忍 'Z' 后缀(UTC ISO 8601)。"""
    dt = _parse_iso("2026-07-14T10:00:00Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 14


# ---------------------------------------------------------------------------
# ValidationIssue / TodoEvent / RuleId
# ---------------------------------------------------------------------------


def test_validation_issue_construction():
    issue = ValidationIssue(
        task_id="abc12345",
        severity="error",
        rule_id="missing_dependency",
        message="refers to non-existent task xyz",
    )
    assert issue.task_id == "abc12345"
    assert issue.severity == "error"
    assert issue.rule_id == "missing_dependency"
    assert "xyz" in issue.message


def test_validation_issue_global_task_id_none():
    """全局 issue(非 task 级别)task_id 应允许 None。"""
    issue = ValidationIssue(
        task_id=None,
        severity="error",
        rule_id="duplicate_id",
        message="two tasks share id abc",
    )
    assert issue.task_id is None


def test_validation_issue_rule_id_set():
    """8 个 RuleId 字面量集合完整(防拼写漂移)。"""
    from typing import get_args

    ids = set(get_args(RuleId))
    expected = {
        "missing_dependency", "cycle", "self_parent", "missing_parent",
        "orphan_md", "missing_md", "duplicate_id", "dangling_parent",
    }
    assert ids == expected
    assert len(ids) == 8


def test_todo_event_status_changed_has_prev():
    e = TodoEvent(kind="status_changed", prev_status="pending")
    assert e.kind == "status_changed"
    assert e.prev_status == "pending"


def test_todo_event_other_kinds_prev_optional():
    e = TodoEvent(kind="created")
    assert e.prev_status is None

    e2 = TodoEvent(kind="updated")
    assert e2.prev_status is None

    e3 = TodoEvent(kind="deleted")
    assert e3.prev_status is None


# ---------------------------------------------------------------------------
# Manifest(组件 1)
# ---------------------------------------------------------------------------


def test_manifest_minimal_required_fields():
    """只填 4 个必填字段,其他走默认。"""
    m = Manifest(
        project_id="7f3a-2b8c-a91d",
        name="cc-harness",
        todos_path=".cc-harness/todos",
        created_at=datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert m.project_id == "7f3a-2b8c-a91d"
    assert m.name == "cc-harness"
    assert m.todos_path == ".cc-harness/todos"
    assert m.schema_version == 1
    assert m.resume_mode == "ask"
    assert m.memory.integration.completion_capture is False
    assert m.live.position == "top"
    assert m.live.max_height == 10


def test_manifest_full_fields():
    """填满所有 manifest 字段。"""
    m = Manifest(
        project_id="p1",
        name="x",
        todos_path="todos",
        created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
        schema_version=1,
        memory=MemoryConfig(
            db_path="logs/memory.db",
            integration=MemoryIntegrationConfig(completion_capture=True),
        ),
        resume_mode="auto",
        live=LiveConfig(
            position="bottom",
            max_height=15,
            spinner_style="arrow",
            show_progress_bar=False,
            fold_done=3,
        ),
    )
    assert m.memory.db_path == "logs/memory.db"
    assert m.memory.integration.completion_capture is True
    assert m.resume_mode == "auto"
    assert m.live.position == "bottom"
    assert m.live.fold_done == 3
    assert m.live.show_progress_bar is False


def test_manifest_resume_mode_values():
    """ResumeMode 字面量必须包含 ask / auto / manual。"""
    from typing import get_args

    modes = set(get_args(__import__("cc_harness.project.models", fromlist=["ResumeMode"]).ResumeMode))
    assert modes == {"ask", "auto", "manual"}


# ---------------------------------------------------------------------------
# Parametrized — Status / Priority 字面量完整性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["pending", "in_progress", "done", "blocked", "cancelled"],
)
def test_todo_task_accepts_all_status_literals(status):
    now = datetime.now(timezone.utc)
    t = TodoTask(
        id=f"id-{status}",
        title="t",
        status=status,
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
    assert t.status == status


@pytest.mark.parametrize(
    "priority",
    ["low", "medium", "high", "critical", None],
)
def test_todo_task_accepts_all_priorities(priority):
    now = datetime.now(timezone.utc)
    t = TodoTask(
        id=f"id-{priority}",
        title="t",
        status="pending",
        description="",
        depends_on=[],
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
    assert t.priority == priority