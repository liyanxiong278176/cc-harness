"""B-stage integration coverage for the todo DAG and verify-hint pipeline."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cc_harness.agent import run_turn
from cc_harness.cli.init import init_noninteractive
from cc_harness.llm import PendingToolCall
from cc_harness.policy import PolicyEngine
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService
from cc_harness.repl import (
    MAX_HINTS_PER_TASK,
    MAX_HINTS_TOTAL,
    ReplState,
    _after_turn_todo,
    _extract_final_text,
)
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent


async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


def _make_service(tmp_path: Path) -> TodoService:
    manifest = init_noninteractive(
        tmp_path,
        name="b-integration",
        write_gitignore=False,
    )
    return TodoService(project_root=tmp_path, manifest=manifest)


def _record_llm_calls(llm: FakeLLM) -> tuple[list[str], list[list[dict]]]:
    """Wrap a shared FakeLLM while preserving responses and recording inputs."""
    prompts: list[str] = []
    tool_specs_seen: list[list[dict]] = []
    original_chat = llm.chat

    async def recording_chat(messages, tools):
        prompts.append(messages[0].get("content", "") if messages else "")
        tool_specs_seen.append(list(tools or []))
        async for event in original_chat(messages, tools):
            yield event

    llm.chat = recording_chat
    return prompts, tool_specs_seen


async def test_b_e2e_llm_uses_topo_sort(tmp_path: Path):
    """A fake ReAct turn exposes all todo tools and consumes the DAG render."""
    svc = _make_service(tmp_path)
    first = await _create(svc, "bootstrap task", session_id="e2e")
    second = await _create(
        svc,
        "implement feature",
        status="in_progress",
        deps=[first.id],
        session_id="e2e",
    )
    third = await _create(
        svc,
        "verify feature",
        deps=[second.id],
        session_id="e2e",
    )

    pending = [
        PendingToolCall(
            index=0,
            id="topo-1",
            name="todo_toposort",
            arguments_json=json.dumps({"group": "all"}),
        )
    ]
    llm = FakeLLM(
        responses=[
            [
                FakeStreamEvent(
                    kind="done",
                    content="I will inspect the task graph first.",
                    pending=pending,
                    finish_reason="tool_calls",
                )
            ],
            [
                FakeStreamEvent(
                    kind="done",
                    content="The task graph is ordered and ready for execution.",
                    pending=[],
                    finish_reason="stop",
                )
            ],
        ]
    )
    prompts, tool_specs_seen = _record_llm_calls(llm)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    extras = inject_todo_tools(svc, session_id="e2e", cwd=str(tmp_path))
    messages = [{"role": "user", "content": "Inspect the project task graph."}]

    await run_turn(
        messages,
        llm,
        mcp,
        cwd=str(tmp_path),
        max_iter=3,
        extra_native_specs=extras,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )

    todo_names = {
        entry["spec"]["function"]["name"]
        for entry in extras
    }
    observed_names = {
        spec["function"]["name"]
        for spec in tool_specs_seen[0]
    }
    # FakeLLM sees all 9 todo specs (D1 Task 5); run_command remains the 10th built-in.
    assert len(prompts) == 2
    assert len(todo_names) == 9
    assert todo_names <= observed_names
    assert observed_names - todo_names == {"run_command"}
    assert len(messages) == 5
    tool_call_message = next(
        message
        for message in messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert tool_call_message["tool_calls"][0]["function"]["name"] == "todo_toposort"
    assert json.loads(tool_call_message["tool_calls"][0]["function"]["arguments"]) == {
        "group": "all"
    }
    topo_result = next(
        message["content"]
        for message in messages
        if message.get("role") == "tool"
    )
    assert "DAG 拓扑视图" in topo_result
    assert first.id in topo_result
    assert second.id in topo_result
    assert third.id in topo_result
    assert messages[-1]["content"] == "The task graph is ordered and ready for execution."
    assert llm.call_count == 2
    assert mcp.calls == []


async def test_b_e2e_verify_hints_influence_next_turn(tmp_path: Path):
    """A turn's verify result is visible in the next turn's system prompt."""
    svc = _make_service(tmp_path)
    await _create(
        svc,
        "run acceptance checks",
        status="in_progress",
        criteria=["run the unit tests"],
        session_id="hints",
    )
    llm = FakeLLM(
        responses=[
            [
                FakeStreamEvent(
                    kind="done",
                    content="I inspected the project files.",
                    pending=[],
                    finish_reason="stop",
                )
            ],
            [
                FakeStreamEvent(
                    kind="done",
                    content="I will run the unit tests now.",
                    pending=[],
                    finish_reason="stop",
                )
            ],
        ]
    )
    system_prompts, _ = _record_llm_calls(llm)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "Work on the acceptance checks."}]
    state = ReplState(todo_service=svc)

    await run_turn(
        messages,
        llm,
        mcp,
        cwd=str(tmp_path),
        max_iter=1,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )
    assert messages[-1]["content"] == "I inspected the project files."
    state.last_turn_text = _extract_final_text(messages)
    await _after_turn_todo(state, svc)

    assert state.todo_hints
    assert any("run the unit tests" in hint for hint in state.todo_hints)

    await run_turn(
        messages,
        llm,
        mcp,
        cwd=str(tmp_path),
        max_iter=1,
        todo_hints=list(state.todo_hints),
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )

    assert len(system_prompts) == 2
    assert "<todo_hints>" in system_prompts[1]
    assert "run the unit tests" in system_prompts[1]


async def test_b_e2e_hints_truncation_two_layer(tmp_path: Path):
    """Verify output applies the per-task cap before the global ten-hint cap."""
    svc = _make_service(tmp_path)
    tasks = []
    for index in range(12):
        tasks.append(
            await _create(
                svc,
                f"in-progress task {index}",
                status="in_progress",
                criteria=[f"deliverable_{index}_{criterion}" for criterion in range(4)],
                session_id="truncate",
            )
        )

    state = ReplState(todo_service=svc, last_turn_text="nothing relevant was produced")
    await _after_turn_todo(state, svc)

    assert len(state.todo_hints) == MAX_HINTS_TOTAL
    assert state.todo_hints[:10] == state.todo_hints
    hints_by_task = Counter(
        task.id
        for task in tasks
        for hint in state.todo_hints
        if f"task {task.id} " in hint
    )
    assert hints_by_task
    assert all(count <= MAX_HINTS_PER_TASK for count in hints_by_task.values())
    expected = [
        f"task {task.id} criterion 未在最近一轮输出中体现: {criterion}"
        for task in tasks
        for criterion in task.acceptance_criteria[:MAX_HINTS_PER_TASK]
    ]
    assert state.todo_hints == expected[:MAX_HINTS_TOTAL]
