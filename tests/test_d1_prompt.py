"""D1 Task 6: <subagent_hints> 静态提示注入(coding mode + HTN parent 已创建)。"""
from __future__ import annotations

import pytest

from cc_harness.agent import _refresh_system_prompt


def _htn_parent_create_tool_message(parent_task_id: str) -> dict:
    """模拟 LLM 调 todo_create(title=..., parent_task=parent_task_id) 后的 tool message。"""
    return {
        "role": "tool",
        "name": "todo_create",
        "content": f'{{"id": "t1", "title": "x", "parent_task": "{parent_task_id}"}}',
    }


def test_subagent_hints_injected_after_htn_parent_create(tmp_path):
    """messages 含 todo_create + parent_task 非 None → system prompt 末有 <subagent_hints>。"""
    messages = [
        {"role": "user", "content": "x"},
        _htn_parent_create_tool_message("p1"),
    ]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<subagent_hints>" in messages[0]["content"]
    assert "len(sub_specs)" in messages[0]["content"]  # 关键澄清:N = len(sub_specs)


def test_subagent_hints_not_injected_in_plan_mode(tmp_path):
    messages = [{"role": "user", "content": "x"}, _htn_parent_create_tool_message("p1")]
    _refresh_system_prompt(messages, str(tmp_path), "plan")
    assert "<subagent_hints>" not in messages[0]["content"]


def test_subagent_hints_not_injected_without_htn_parent(tmp_path):
    """messages 无 HTN parent create → 不注入(避免 false positive)。"""
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "tool",
            "name": "todo_create",
            "content": '{"id": "t1", "title": "x", "parent_task": null}',
        },
    ]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<subagent_hints>" not in messages[0]["content"]


def test_subagent_hints_idempotent(tmp_path):
    """连续 refresh → <subagent_hints> 仍只 1 次(类比 <todo_completion_gate>)。"""
    messages = [{"role": "user", "content": "x"}, _htn_parent_create_tool_message("p1")]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    once = messages[0]["content"].count("<subagent_hints>")
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    twice = messages[0]["content"].count("<subagent_hints>")
    assert once == twice == 1


async def _run_real_todo_create(tmp_path):
    """Run a real todo_create(parent_task=...) tool call through run_turn."""
    import json

    from cc_harness.agent import run_turn
    from cc_harness.cli.init import init_noninteractive
    from cc_harness.llm import PendingToolCall
    from cc_harness.policy import PolicyEngine
    from cc_harness.project.service import TodoService
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent

    service = TodoService(
        project_root=tmp_path,
        manifest=init_noninteractive(tmp_path, name="d1-prompt", write_gitignore=False),
    )
    parent = await service.create(title="existing parent", session_id="s")
    pending = [PendingToolCall(
        index=0,
        id="create-child",
        name="todo_create",
        arguments_json=json.dumps({
            "title": "created child",
            "parent_task": parent.id,
        }),
    )]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="create", pending=pending,
                         finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="created", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "create a child"}]
    await run_turn(
        messages,
        llm,
        mcp,
        cwd=str(tmp_path),
        max_iter=3,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
        todo_service=service,
        session_id="s",
    )
    return messages


@pytest.mark.asyncio
async def test_d1_prompt_detects_htn_parent_from_real_tool_message(tmp_path):
    """Real dispatcher output has a tool name but plain-text handler content."""
    messages = await _run_real_todo_create(tmp_path)
    tool_message = next(m for m in messages if m.get("role") == "tool")
    assert tool_message["name"] == "todo_create"
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<subagent_hints>" in messages[0]["content"]
