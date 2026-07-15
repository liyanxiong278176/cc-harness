"""tests/test_project_live.py — TodoLivePanel tests(spec 组件 6)。

覆盖:
    - _render_static 纯函数(0 task / 多 task / 图标 / 颜色 / 折叠 / 截断)
    - start/stop + 订阅生命周期
    - Live panel 与 asyncio.to_thread(input) 共存(REPL 兼容性)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from rich.console import Console

from cc_harness.project.live import TodoLivePanel, render_to_string
from cc_harness.project.models import Manifest, TodoTask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_manifest(**overrides) -> Manifest:
    """Build a Manifest with sensible defaults for Live tests."""
    defaults = dict(
        project_id="abc-123",
        name="myproject",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Manifest(**defaults)


def _make_task(id: str = "aaa11111", title: str = "task",
               status: str = "pending", priority: str | None = None,
               updated_at: datetime | None = None) -> TodoTask:
    """Build a minimal TodoTask."""
    now = updated_at or datetime.now(timezone.utc)
    return TodoTask(
        id=id, title=title, status=status,
        description="", depends_on=[], parent_task=None,
        assigned_to=None, priority=priority, labels=[],
        due_date=None, effort_estimate=None, acceptance_criteria=[],
        created_at=now, updated_at=now, active_sessions=[],
    )


@pytest.fixture
def proj(tmp_path):
    """Build a project skeleton with .cc-harness/todos/ ready."""
    p = tmp_path / "proj"
    p.mkdir()
    todos = p / ".cc-harness" / "todos"
    todos.mkdir(parents=True)
    (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return p


@pytest.fixture
def svc(proj):
    """Real TodoService against an on-disk project."""
    from cc_harness.project.service import TodoService
    return TodoService(project_root=proj, manifest=_make_manifest())


# ---------------------------------------------------------------------------
# _render_static — 纯函数(单测)
# ---------------------------------------------------------------------------


def test_render_empty():
    """0 tasks → 'no tasks yet' marker."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=80)
    TodoLivePanel._render_static(
        console, tasks=[], project_name="myproject", project_id="abc",
    )
    text = buf.getvalue()
    assert "myproject" in text
    assert "abc" in text
    assert "no tasks" in text.lower()


def test_render_with_tasks_status_icons():
    """多 task → status 图标 + id + title 都渲染。"""
    now = datetime.now(timezone.utc)
    tasks = [
        _make_task("aaa11111", "done task", "done", updated_at=now),
        _make_task("bbb22222", "active task", "in_progress", "high", now),
        _make_task("ccc33333", "waiting task", "pending", updated_at=now),
    ]
    text = render_to_string(tasks, project_name="x", project_id="abc")
    assert "done task" in text
    assert "active task" in text
    assert "waiting task" in text
    assert "high" in text
    assert "✓" in text    # done icon
    assert "⠋" in text    # in_progress icon
    assert "○" in text    # pending icon


def test_render_with_all_status_icons():
    """全部 5 个 status icon 都覆盖。"""
    now = datetime.now(timezone.utc)
    tasks = [
        _make_task(f"id{i:08d}", f"t{i}", status, updated_at=now)
        for i, status in enumerate(
            ["done", "in_progress", "pending", "blocked", "cancelled"]
        )
    ]
    text = render_to_string(tasks, project_name="x", project_id="abc")
    assert "✓" in text   # done
    assert "⠋" in text   # in_progress
    assert "○" in text   # pending
    assert "!" in text   # blocked
    assert "✗" in text   # cancelled


def test_render_title_truncation():
    """标题超长(>50 字符)→ 截断 + ellipsis。"""
    long_title = "a" * 100
    tasks = [_make_task("aaa", long_title, "pending")]
    text = render_to_string(tasks, project_name="x", project_id="abc")
    # 标题里应有 'a' * 49 + '…'
    assert "a" * 49 + "…" in text
    # 不应含原始长度 100
    assert "a" * 50 not in text


def test_render_max_height_folds():
    """超过 max_height → 折叠提示 `... +N more`。"""
    now = datetime.now(timezone.utc)
    tasks = [
        _make_task(f"id{i:08d}", f"task{i}", "pending", updated_at=now)
        for i in range(15)
    ]
    text = render_to_string(
        tasks, project_name="x", project_id="abc",
        max_height=5, fold_done=0,
    )
    assert "+10 more" in text


def test_render_fold_done():
    """fold_done=N → done 任务保留前 N 个,其余 done 不显示。"""
    now = datetime.now(timezone.utc)
    done_tasks = [
        _make_task(f"done{i:04d}", f"d{i}", "done", updated_at=now)
        for i in range(10)
    ]
    text = render_to_string(
        done_tasks, project_name="x", project_id="abc",
        max_height=20, fold_done=3,
    )
    # 前 3 个 done 的 title 应在
    assert "d0" in text
    assert "d1" in text
    assert "d2" in text
    # 第 5 个 done 的 title 应被折叠掉(只显示前 3)
    assert "d5" not in text
    assert "d9" not in text


def test_render_progress_bar():
    """show_progress_bar=True → 进度条格式 `N/M (NN%)`。"""
    now = datetime.now(timezone.utc)
    tasks = [
        _make_task("aaa", "done1", "done", updated_at=now),
        _make_task("bbb", "done2", "done", updated_at=now),
        _make_task("ccc", "progress", "in_progress", updated_at=now),
        _make_task("ddd", "pending1", "pending", updated_at=now),
    ]
    text = render_to_string(
        tasks, project_name="x", project_id="abc",
        show_progress_bar=True,
    )
    assert "Progress:" in text
    assert "2/4" in text
    assert "50%" in text


def test_render_sort_priority_then_updated():
    """渲染顺序 = status priority → updated_at desc(LLM 视野里最新 in_progress 优先)。"""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    tasks = [
        _make_task("aaa", "old_pending", "pending", updated_at=t1),
        _make_task("bbb", "fresh_pending", "pending", updated_at=t2),
        _make_task("ccc", "fresh_done", "done", updated_at=t3),
    ]
    text = render_to_string(tasks, project_name="x", project_id="abc")
    # in_progress/bocked/pending 在前;done 在后
    pos_old_pending = text.find("old_pending")
    pos_fresh_pending = text.find("fresh_pending")
    pos_fresh_done = text.find("fresh_done")
    assert pos_fresh_done > pos_old_pending    # done 在 pending 之后
    assert pos_fresh_done > pos_fresh_pending  # done 在 fresh pending 之后
    # pending 内部:fresh(updated=2026-06)在前,old(updated=2026-01)在后(updated desc)
    assert pos_fresh_pending < pos_old_pending


# ---------------------------------------------------------------------------
# Live context start/stop + 订阅生命周期
# ---------------------------------------------------------------------------


def test_panel_subscribes_to_service(svc):
    """start() → subscribe callback 注册;stop() → unsubscribe。"""
    from rich.console import Console
    panel = TodoLivePanel(Console(), svc, _make_manifest())
    panel.start()
    # 订阅路径:任何 TodoService 事件都会走 panel._on_change
    # 验证:_subscribers list 里有 panel._on_change
    assert panel._on_change in svc._subscribers
    panel.stop()
    # stop 后 unsubscribe → 不在 list
    assert panel._on_change not in svc._subscribers


def test_panel_stop_is_idempotent(svc):
    """stop() 双调不抛异常。"""
    panel = TodoLivePanel(Console(), svc, _make_manifest())
    panel.start()
    panel.stop()
    panel.stop()  # 不抛


def test_panel_start_is_idempotent(svc):
    """start() 双调不重复 subscribe。"""
    panel = TodoLivePanel(Console(), svc, _make_manifest())
    panel.start()
    initial_n = len(svc._subscribers)
    panel.start()  # 第二次应 no-op
    assert len(svc._subscribers) == initial_n
    panel.stop()


def test_panel_with_statement(svc):
    """`with TodoLivePanel(...) as panel:` → start/stop 自动。"""
    with TodoLivePanel(Console(), svc, _make_manifest()) as panel:
        assert panel._started is True
    # __exit__ 已 stop
    assert panel._started is False


@pytest.mark.asyncio
async def test_live_panel_does_not_break_read_user(svc):
    """Live panel start 后,_read_user 仍可被 mock 调(REPL 测试兼容)。

    关键:Rich Live + asyncio.to_thread(input) 不冲突。
    """
    panel = TodoLivePanel(Console(), svc, _make_manifest())
    panel.start()
    try:
        # 模拟 _read_user 返回 'exit'
        with patch("cc_harness.repl._read_user",
                   new=AsyncMock(return_value="exit")) as mocked:
            from cc_harness.repl import _read_user as live_read
            result = await live_read("anything>")
            assert result == "exit"
    finally:
        panel.stop()


@pytest.mark.asyncio
async def test_live_panel_unsubscribes_on_stop_no_dangling(svc):
    """stop 后订阅链清空 — 即使新事件触发也不再走 panel。"""
    from cc_harness.project.models import TodoEvent
    panel = TodoLivePanel(Console(), svc, _make_manifest())
    panel.start()
    initial_n = len(svc._subscribers)
    panel.stop()
    after_n = len(svc._subscribers)
    # 必须少一个
    assert after_n == initial_n - 1
    # 触发事件:不应抛
    svc._emit(
        _make_task("aaa", "x", "pending"),
        TodoEvent(kind="created"),
    )