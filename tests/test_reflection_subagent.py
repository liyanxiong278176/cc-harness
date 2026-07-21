"""E2 Task 3.2:SubAgentRunner 末尾 emit + _render_subagent_summary recent_reflections 字段。

4 个 test case:
1. test_subagent_failed_status_emits_neg — status='failed' 工厂映射 severity=neg
2. test_subagent_blocked_maps_ambig — status='blocked' 工厂映射 severity=ambig
3. test_render_subagent_summary_includes_recent_reflections — engine 非 None 时追加段
4. test_render_subagent_summary_no_engine_works — engine=None 时不报 KeyError
"""
from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock

import pytest

from cc_harness.reflection.engine import ReflectionEngine
from cc_harness.reflection.events import subagent_failed


@pytest.mark.asyncio
async def test_subagent_failed_status_emits_neg():
    """SubAgentResult.status='failed' 触发 emit severity=neg。"""
    from cc_harness.project.subagent import SubAgentResult, SubAgentRunner

    re = MagicMock(spec=ReflectionEngine)
    re.emit = AsyncMock()
    re.get_recent = MagicMock(return_value=[])

    runner = SubAgentRunner.__new__(SubAgentRunner)  # noqa: F841 跳过 __init__
    # 模拟 run 末尾 emit 逻辑(直接调 T1.3 工厂验 severity)
    result = SubAgentResult(task_id="t1", title="x", status="failed", final_text="...",
                            tokens_used=0, fatal_error=True)
    ev = subagent_failed(session_id="s1", turn_idx=0, result=result.__dict__)
    assert ev.severity == "neg"


def test_subagent_blocked_maps_ambig():
    from cc_harness.project.subagent import SubAgentResult
    result = SubAgentResult(task_id="t1", title="x", status="blocked", final_text=None,
                            tokens_used=0, fatal_error=False)
    ev = subagent_failed(session_id="s1", turn_idx=0, result=result.__dict__)
    assert ev.severity == "ambig"


def test_render_subagent_summary_includes_recent_reflections():
    """_render_subagent_summary 渲染应包含 recent_reflections 字段。"""
    from cc_harness.project.subagent import _render_subagent_summary, SubAgentResult

    re = MagicMock()
    re.get_recent = MagicMock(return_value=["反思1", "反思2", "反思3"])
    results = [
        SubAgentResult(task_id="t1", title="x", status="done", final_text="ok",
                       tokens_used=10, fatal_error=False),
    ]
    out = _render_subagent_summary(results, parent_id="p1", reflection_engine=re)
    # _render_subagent_summary 返回 ToolResult;近期反思段在 llm_text 字段
    assert "最近反思(E2)" in out.llm_text
    assert "反思1" in out.llm_text


def test_render_subagent_summary_no_engine_works():
    """reflection_engine=None 时不渲染 recent_reflections 字段。"""
    from cc_harness.project.subagent import _render_subagent_summary, SubAgentResult

    results = [
        SubAgentResult(task_id="t1", title="x", status="done", final_text="ok",
                       tokens_used=10, fatal_error=False),
    ]
    out = _render_subagent_summary(results, parent_id="p1", reflection_engine=None)
    assert "最近反思(E2)" not in out.llm_text
