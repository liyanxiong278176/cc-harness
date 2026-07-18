"""D1 Task 8: 集成测试 — dispatch_subagent 完整 ReAct loop + 摘要 + 完成门。"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from cc_harness.agent import run_turn
from cc_harness.cli.init import init_noninteractive
from cc_harness.llm import PendingToolCall
from cc_harness.policy import PolicyEngine
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService
from cc_harness.project.subagent import SubAgentRunner
from cc_harness.project.tools import dispatch_subagent_handler, todo_update_handler
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent


def _make_service(tmp_path: Path) -> TodoService:
    manifest = init_noninteractive(tmp_path, name="d1-int", write_gitignore=False)
    return TodoService(project_root=tmp_path, manifest=manifest)


async def _create(
    svc: TodoService,
    title: str,
    status: str = "pending",
    session_id: str = "s",
):
    """Create a task, using legal pending -> in_progress -> done transitions."""
    task = await svc.create(title=title, session_id=session_id)
    if status == "in_progress":
        task = await svc.update(task.id, status="in_progress", session_id=session_id)
    elif status == "done":
        task = await svc.update(task.id, status="in_progress", session_id=session_id)
        task = await svc.update(task.id, status="done", session_id=session_id)
    return task


def _dynamic_dispatch_llm(
    parent_id: str,
    sub_specs: list[dict],
    *,
    blocked_titles: set[str] | None = None,
    failed_titles: set[str] | None = None,
    leave_pending_titles: set[str] | None = None,
    measure_parallel: bool = False,
):
    """Build a FakeLLM whose child responses are keyed by their runtime todo IDs.

    ``dispatch_subagent`` creates child IDs inside the handler, so a small chat
    adapter on top of the shared FakeLLM reads each child's ID from its system
    prompt before returning its tool calls.  This keeps the test on the real
    ReAct/runner/handler path without redefining the shared test double.
    """
    blocked_titles = blocked_titles or set()
    failed_titles = failed_titles or set()
    leave_pending_titles = leave_pending_titles or set()
    llm = FakeLLM(responses=[])
    child_calls: dict[str, int] = {}
    state = {"active": 0, "max_active": 0, "main_calls": 0}

    async def chat(messages, tools):
        del tools
        llm.call_count += 1
        system = messages[0].get("content", "") if messages else ""
        if "# SubAgent 上下文" not in system:
            call_no = state["main_calls"]
            state["main_calls"] += 1
            if call_no == 0:
                pending = PendingToolCall(
                    index=0,
                    id="dispatch-main",
                    name="dispatch_subagent",
                    arguments_json=json.dumps({
                        "task_id": parent_id,
                        "sub_specs": sub_specs,
                    }),
                )
                yield FakeStreamEvent(
                    kind="done", content="fan out", pending=[pending],
                    finish_reason="tool_calls",
                )
            else:
                yield FakeStreamEvent(
                    kind="done", content="parent received the subagent summary",
                    pending=[], finish_reason="stop",
                )
            return

        task_id = re.search(r"^- task_id: (.+)$", system, re.MULTILINE).group(1)
        title = re.search(r"^- title: (.+)$", system, re.MULTILINE).group(1)
        call_no = child_calls.get(task_id, 0)
        child_calls[task_id] = call_no + 1

        if title in failed_titles:
            raise RuntimeError(f"simulated failure for {title}")

        if call_no == 0 and title not in leave_pending_titles:
            if measure_parallel:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                await asyncio.sleep(0.02)
                state["active"] -= 1

            if title in blocked_titles:
                status_calls = [
                    PendingToolCall(
                        index=0,
                        id=f"{task_id}-blocked",
                        name="todo_update",
                        arguments_json=json.dumps({
                            "task_id": task_id,
                            "status": "blocked",
                        }),
                    )
                ]
                text = f"blocked {title}"
            else:
                status_calls = [
                    PendingToolCall(
                        index=0,
                        id=f"{task_id}-progress",
                        name="todo_update",
                        arguments_json=json.dumps({
                            "task_id": task_id,
                            "status": "in_progress",
                        }),
                    ),
                    PendingToolCall(
                        index=1,
                        id=f"{task_id}-done",
                        name="todo_update",
                        arguments_json=json.dumps({
                            "task_id": task_id,
                            "status": "done",
                        }),
                    ),
                ]
                text = f"completed {title}"
            yield FakeStreamEvent(
                kind="done", content=text, pending=status_calls,
                finish_reason="tool_calls",
            )
            return

        yield FakeStreamEvent(
            kind="done", content=f"final result for {title} (tests/{title}.py)",
            pending=[], finish_reason="stop",
        )

    llm.chat = chat
    return llm, state


async def _run_dispatch(
    svc: TodoService,
    parent_id: str,
    sub_specs: list[dict],
    **llm_options,
):
    llm, state = _dynamic_dispatch_llm(parent_id, sub_specs, **llm_options)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "fan out the independent work"}]
    await run_turn(
        messages,
        llm,
        mcp,
        cwd=str(svc.project_root),
        max_iter=5,
        policy=PolicyEngine(project_root=svc.project_root, enabled=False),
        todo_service=svc,
        session_id="s",
    )
    return messages, state, mcp


@pytest.mark.asyncio
async def test_d1_dispatch_3_subagents_parallel_fake_llm(tmp_path: Path):
    """3 个 subagent 真并行 + 摘要渲染 + 全部 done 后父完成门放行。"""
    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    specs = [
        {"title": "child-1", "criteria": ["criterion 1"]},
        {"title": "child-2", "criteria": ["criterion 2"]},
        {"title": "child-3", "criteria": ["criterion 3"]},
    ]

    messages, state, mcp = await _run_dispatch(
        svc, parent.id, specs, measure_parallel=True,
    )

    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert tool_messages
    summary = tool_messages[-1]["content"]
    assert "N=3" in summary
    assert "全部 done" in summary
    assert state["max_active"] == 3
    assert mcp.calls == []

    children = await svc.list(parent_task=parent.id)
    assert len(children) == 3
    assert {child.status for child in children} == {"done"}
    assert all(child.acceptance_criteria == [] for child in children)

    await svc.update(parent.id, status="in_progress", session_id="s")
    result = await todo_update_handler(
        {"task_id": parent.id, "status": "done"},
        service=svc,
        session_id="s",
        cwd=str(tmp_path),
    )
    assert result.is_error is False
    assert (await svc.get(parent.id)).status == "done"


@pytest.mark.asyncio
async def test_d1_dispatch_with_subagent_failure(tmp_path: Path):
    """1 个 subagent 失败,其他两个仍完成并进入同一份摘要。"""
    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    specs = [
        {"title": "ok-1"},
        {"title": "failed-child"},
        {"title": "ok-2"},
    ]

    messages, _state, _mcp = await _run_dispatch(
        svc,
        parent.id,
        specs,
        failed_titles={"failed-child"},
    )

    summary = [m for m in messages if m.get("role") == "tool"][-1]["content"]
    assert "状态=failed" in summary
    assert summary.count("状态=done") == 2
    assert "父任务" in summary and "不可标 done" in summary

    children = await svc.list(parent_task=parent.id)
    statuses = {child.title: child.status for child in children}
    assert statuses["ok-1"] == "done"
    assert statuses["ok-2"] == "done"
    # The failed runner reports failed without falsely marking its todo done.
    assert statuses["failed-child"] == "pending"


@pytest.mark.asyncio
async def test_d1_dispatch_subagent_uses_completion_gate_aggregation(tmp_path: Path):
    """父完成门聚合真实 sub-todo 状态,子任务未 done 时拒绝父 done。"""
    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    specs = [{"title": "unfinished-child"}]

    messages, _state, _mcp = await _run_dispatch(
        svc,
        parent.id,
        specs,
        leave_pending_titles={"unfinished-child"},
    )
    summary = [m for m in messages if m.get("role") == "tool"][-1]["content"]
    assert "未 done" in summary

    child = (await svc.list(parent_task=parent.id))[0]
    assert child.status == "pending"
    await svc.update(parent.id, status="in_progress", session_id="s")
    blocked = await todo_update_handler(
        {"task_id": parent.id, "status": "done"},
        service=svc,
        session_id="s",
        cwd=str(tmp_path),
    )
    assert blocked.is_error is True
    assert "子任务" in blocked.llm_text
    assert child.id in blocked.llm_text
    assert (await svc.get(parent.id)).status == "in_progress"

    await svc.update(child.id, status="in_progress", session_id="s")
    await svc.update(child.id, status="done", session_id="s")
    passed = await todo_update_handler(
        {"task_id": parent.id, "status": "done"},
        service=svc,
        session_id="s",
        cwd=str(tmp_path),
    )
    assert passed.is_error is False
    assert (await svc.get(parent.id)).status == "done"


@pytest.mark.asyncio
async def test_d1_dispatch_subagent_creates_correct_parent_child(tmp_path: Path):
    """dispatch 创建的每个 sub-todo 都指向调用中的 parent_task 且不继承 criteria。"""
    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    specs = [
        {"title": "child-a", "criteria": ["should not be persisted"]},
        {"title": "child-b", "description": "independent work"},
    ]

    await _run_dispatch(
        svc,
        parent.id,
        specs,
        leave_pending_titles={"child-a", "child-b"},
    )

    children = await svc.list(parent_task=parent.id)
    assert {child.title for child in children} == {"child-a", "child-b"}
    assert all(child.parent_task == parent.id for child in children)
    assert all(child.acceptance_criteria == [] for child in children)


@pytest.mark.asyncio
async def test_d1_three_level_nested_blocked(tmp_path: Path):
    """depth=2 调 dispatch_subagent 时硬拒,且不创建第四层 sub-todo。"""
    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    runner_depth2 = SubAgentRunner(
        llm=FakeLLM(responses=[]),
        mcp=FakeMCP(tools_spec=[], results={}, calls=[]),
        todo_service=svc,
        current_depth=2,
        project_root=str(tmp_path),
        max_iter=5,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )
    extras = inject_todo_tools(
        svc,
        session_id="s",
        cwd=str(tmp_path),
        dispatch_subagent_runner=runner_depth2,
    )
    dispatch_entry = next(
        entry for entry in extras
        if entry["spec"]["function"]["name"] == "dispatch_subagent"
    )

    result = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "too-deep"}]},
        **dispatch_entry["deps"],
    )

    assert result.is_error is True
    text = (result.display_text or "") + (result.llm_text or "")
    assert "max_depth=2" in text
    assert await svc.list(parent_task=parent.id) == []


@pytest.mark.asyncio
async def test_d1_dispatch_summarizes_blocked_state_for_parent(tmp_path: Path):
    """blocked child 在摘要中可见,并给出父任务不可完成的决策路径。"""
    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    specs = [{"title": "blocked-child"}]

    messages, _state, _mcp = await _run_dispatch(
        svc,
        parent.id,
        specs,
        blocked_titles={"blocked-child"},
    )

    summary = [m for m in messages if m.get("role") == "tool"][-1]["content"]
    assert "状态=blocked" in summary
    assert "父完成门" in summary
    assert f"父任务 {parent.id} 不可标 done" in summary

    child = (await svc.list(parent_task=parent.id))[0]
    assert child.status == "blocked"
    await svc.update(parent.id, status="in_progress", session_id="s")
    parent_result = await todo_update_handler(
        {"task_id": parent.id, "status": "done"},
        service=svc,
        session_id="s",
        cwd=str(tmp_path),
    )
    assert parent_result.is_error is True
    assert child.id in parent_result.llm_text
    assert "子任务" in parent_result.llm_text
