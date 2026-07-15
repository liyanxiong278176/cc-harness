"""memory_bridge 测试(spec 组件 10)。

覆盖:
- completion_capture=False → no-op
- completion_capture=True + memory_service=None → no-op
- completion_capture=True + memory_service 存在 → 调 save(检查参数)
- save 抛异常 → swallow
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cc_harness.project.memory_bridge import on_task_completion
from cc_harness.project.models import Manifest, TodoTask


def _task(
    id: str = "abc12345",
    title: str = "hello",
    active_sessions: list[str] | None = None,
) -> TodoTask:
    now = datetime.now(timezone.utc)
    return TodoTask(
        id=id,
        title=title,
        status="done",
        created_at=now,
        updated_at=now,
        description="",
        depends_on=[],
        parent_task=None,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        active_sessions=list(active_sessions or []),
    )


def _manifest(completion_capture: bool = False) -> Manifest:
    return Manifest(
        project_id="x",
        name="x",
        todos_path=".",
        created_at=datetime.now(timezone.utc),
        memory=replace(
            Manifest(
                project_id="x", name="x", todos_path=".",
                created_at=datetime.now(timezone.utc),
            ).memory,
            integration=replace(
                Manifest(
                    project_id="x", name="x", todos_path=".",
                    created_at=datetime.now(timezone.utc),
                ).memory.integration,
                completion_capture=completion_capture,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 基础 no-op 行为
# ---------------------------------------------------------------------------


async def test_completion_capture_disabled_returns() -> None:
    """plan lines 573-578:completion_capture=False(默认)→ mem.save 不调。"""
    m = _manifest(completion_capture=False)
    mem = AsyncMock()
    await on_task_completion(_task(), m, mem)
    mem.save.assert_not_called()


async def test_no_memory_service_returns() -> None:
    """plan lines 580-583:memory_service=None → 不抛错,no-op。"""
    m = _manifest(completion_capture=True)
    await on_task_completion(_task(), m, None)  # 不抛


async def test_disabled_and_no_service() -> None:
    """双重 None 兜底。"""
    m = _manifest(completion_capture=False)
    await on_task_completion(_task(), m, None)


# ---------------------------------------------------------------------------
# enabled 行为
# ---------------------------------------------------------------------------


async def test_completion_capture_enabled_calls_save() -> None:
    """plan lines 580-583 风格:验证 save 参数。"""
    m = _manifest(completion_capture=True)
    mem = AsyncMock()
    t = _task(id="abc12345", title="hello", active_sessions=["sess-A"])
    await on_task_completion(t, m, mem)
    mem.save.assert_awaited_once()
    args, kwargs = mem.save.call_args
    text = args[0] if args else kwargs.get("text")
    assert text is not None
    assert "[task done]" in text
    assert "abc12345" in text
    assert "hello" in text
    assert kwargs["source"] == "todo/completion"
    assert kwargs["session_id"] == "sess-A"


async def test_completion_capture_uses_last_active_session() -> None:
    """多个 session → 取最近一个(最后一个)。"""
    m = _manifest(completion_capture=True)
    mem = AsyncMock()
    t = _task(active_sessions=["sess-A", "sess-B", "sess-C"])
    await on_task_completion(t, m, mem)
    assert mem.save.call_args.kwargs["session_id"] == "sess-C"


async def test_completion_capture_no_sessions_passes_none() -> None:
    """active_sessions 为空 → session_id=None。"""
    m = _manifest(completion_capture=True)
    mem = AsyncMock()
    await on_task_completion(_task(), m, mem)
    assert mem.save.call_args.kwargs["session_id"] is None


# ---------------------------------------------------------------------------
# 异常行为
# ---------------------------------------------------------------------------


async def test_save_exception_swallowed() -> None:
    """memory_service.save 抛异常 → on_task_completion 不冒泡(spec line 624)。"""
    m = _manifest(completion_capture=True)
    mem = AsyncMock()
    mem.save.side_effect = RuntimeError("memory boom")
    # 不抛
    await on_task_completion(_task(), m, mem)
    mem.save.assert_awaited_once()


async def test_save_returns_arbitrary_value() -> None:
    """save 的返回值不需特定(可能 SaveResult / None)— 桥只调不解析。"""
    m = _manifest(completion_capture=True)
    mem = AsyncMock()
    mem.save.return_value = "some result"
    await on_task_completion(_task(), m, mem)  # 不抛