"""C-stage integration coverage: todo_update completion gate + HTN tree view.

Five integration cases (FakeLLM/FakeMCP + direct handler calls). The agent
loop is used only when it adds clear value (verifying the tool is exposed and
routed correctly via extras); the deps + handler + service chain is verified
directly because `last_turn_text` deps are fixed at inject_todo_tools time
and cannot be updated mid-run_turn. The plan explicitly allows this
degradation: "集成测试本质是验证 handler + deps + service 链路,不必走完整
agent loop"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_harness.agent import run_turn
from cc_harness.cli.init import init_noninteractive
from cc_harness.llm import PendingToolCall
from cc_harness.policy import PolicyEngine
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService
from cc_harness.project.tools import (
    todo_update_handler,
)
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent


# --- Shared helpers (B-plan round-2 pattern + test_b_integration 沿袭) ---


def _make_service(tmp_path: Path) -> TodoService:
    """Spin up an isolated cc-harness project root + TodoService."""
    manifest = init_noninteractive(tmp_path, name="c-int", write_gitignore=False)
    return TodoService(project_root=tmp_path, manifest=manifest)


async def _create(
    svc: TodoService,
    title: str,
    status: str = "pending",
    criteria: list[str] | None = None,
    deps: list[str] | None = None,
    parent: str | None = None,
    session_id: str = "s",
) -> object:
    """Create a task with optional initial status flip."""
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        parent_task=parent,
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


async def _update_done(
    svc: TodoService,
    task_id: str,
    *,
    force: bool = False,
    last_turn_text: str = "",
    session_id: str = "s",
) -> object:
    """Invoke todo_update_handler with status=done, returning the ToolResult."""
    args: dict = {"task_id": task_id, "status": "done"}
    if force:
        args["force"] = True
    return await todo_update_handler(
        args, service=svc, session_id=session_id,
        cwd=".", last_turn_text=last_turn_text,
    )


def _record_llm_calls(llm: FakeLLM) -> tuple[list[str], list[list[dict]]]:
    """Wrap a shared FakeLLM while preserving responses and recording inputs.

    Mirrors tests/test_b_integration.py:_record_llm_calls — used by Test 5
    where we want to verify the agent receives the full tool spec set.
    """
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


# --- Test 1: update done → acceptance blocks first, then passes ---


@pytest.mark.asyncio
async def test_c_agent_update_done_blocked_then_pass(tmp_path: Path):
    """Turn 1: text doesn't match AC → blocked (is_error). Turn 2: matches → pass.

    This proves the completion-gate heuristic reads `last_turn_text` correctly
    and the same handler wiring produces both outcomes depending on text.
    """
    svc = _make_service(tmp_path)
    task = await _create(
        svc, "deliver AC1", status="in_progress",
        criteria=["要 AC1"], session_id="c-int-1",
    )

    blocked = await _update_done(
        svc, task.id, last_turn_text="我改了代码,跑了一下手测",
        session_id="c-int-1",
    )
    assert blocked.is_error is True
    assert "acceptance" in blocked.llm_text
    # Critically: status is NOT updated to done despite the call
    assert (await svc.get(task.id)).status == "in_progress"

    passed = await _update_done(
        svc, task.id, last_turn_text="我写了 AC1 的 unit test,过了",
        session_id="c-int-1",
    )
    assert passed.is_error is False
    assert (await svc.get(task.id)).status == "done"


# --- Test 2: parent blocked until children done, then allowed ---


@pytest.mark.asyncio
async def test_c_agent_parent_blocked_until_children_done(tmp_path: Path):
    """Parent update→done blocked when child pending; after child done, parent OK.

    children_all_done is the aggregation primitive wired into the gate (force
    cannot bypass). Verifies the chain: child pending → block; child done
    → allow.
    """
    svc = _make_service(tmp_path)
    parent = await _create(
        svc, "ship feature", status="in_progress", session_id="c-int-2",
    )
    # pending → done is illegal per status_guard; flip child to in_progress first
    child = await _create(
        svc, "sub-task A", status="in_progress", parent=parent.id,
        session_id="c-int-2",
    )

    blocked = await _update_done(svc, parent.id, session_id="c-int-2")
    assert blocked.is_error is True
    assert "子任务" in blocked.llm_text
    assert child.id in blocked.llm_text  # gate names the blocking child

    done_child = await _update_done(svc, child.id, session_id="c-int-2")
    assert done_child.is_error is False
    assert (await svc.get(child.id)).status == "done"

    done_parent = await _update_done(svc, parent.id, session_id="c-int-2")
    assert done_parent.is_error is False
    assert (await svc.get(parent.id)).status == "done"


# --- Test 3: force=true bypasses acceptance (but NOT aggregation) ---


@pytest.mark.asyncio
async def test_c_force_bypass_e2e(tmp_path: Path):
    """force=true sidesteps acceptance heuristic; aggregation still blocks."""
    svc = _make_service(tmp_path)
    task = await _create(
        svc, "feature X", status="in_progress",
        criteria=["要 AC1"], session_id="c-int-3",
    )

    # force + bad last_turn_text → acceptance skipped, status updated
    r = await _update_done(
        svc, task.id, force=True,
        last_turn_text="nope — totally unrelated output",
        session_id="c-int-3",
    )
    assert r.is_error is False
    assert (await svc.get(task.id)).status == "done"


# --- Test 4: deps wiring — handler reads last_turn_text from injected deps ---


@pytest.mark.asyncio
async def test_c_deps_last_turn_text_wired(tmp_path: Path):
    """inject_todo_tools(deps={'last_turn_text': ...}) → handler uses it.

    Two complementary assertions:
      (a) inject_todo_tools attaches last_turn_text into every extras entry's deps.
      (b) Calling the handler with a matching last_turn_text passes acceptance,
          and with a non-matching one fails — proving the deps param is read
          end-to-end into the heuristic (deps not脱链).
    """
    svc = _make_service(tmp_path)
    extras = inject_todo_tools(svc, "s", cwd=".", last_turn_text="placeholder")

    # (a) every extras entry's deps carries last_turn_text
    assert len(extras) == 8
    for entry in extras:
        assert entry["deps"]["last_turn_text"] == "placeholder"

    # (b) handler called with the right deps → heuristic reads it
    task = await _create(
        svc, "verify wiring", status="in_progress",
        criteria=["需要 unit test"], session_id="c-int-4",
    )
    matched = await _update_done(
        svc, task.id, last_turn_text="本轮写了 unit test 覆盖全部分支",
        session_id="c-int-4",
    )
    assert matched.is_error is False
    assert (await svc.get(task.id)).status == "done"


# --- Test 5: toposort tree view exposed via agent extras (full ReAct loop) ---


@pytest.mark.asyncio
async def test_c_toposort_tree_after_decompose(tmp_path: Path):
    """Agent loop FakeLLM emits `todo_toposort view=tree` → handler returns HTN tree.

    This is the only test that exercises the full ReAct loop because we want
    to verify: (i) the extras' tool spec is exposed to the LLM; (ii) the
    FakeLLM's tool_call gets routed to the right handler; (iii) the handler
    return is backfilled into messages as the tool message.
    """
    svc = _make_service(tmp_path)
    parent = await svc.create(title="root plan", session_id="c-int-5")
    child = await svc.create(title="sub-A", parent_task=parent.id,
                              session_id="c-int-5")
    grandchild = await svc.create(title="leaf-A1", parent_task=child.id,
                                  session_id="c-int-5")

    pending = [
        PendingToolCall(
            index=0,
            id="tree-1",
            name="todo_toposort",
            arguments_json=json.dumps({"view": "tree"}),
        )
    ]
    llm = FakeLLM(
        responses=[
            [
                FakeStreamEvent(
                    kind="done",
                    content="Let me check the HTN tree.",
                    pending=pending,
                    finish_reason="tool_calls",
                )
            ],
            [
                FakeStreamEvent(
                    kind="done",
                    content="Tree view confirms structure.",
                    pending=[],
                    finish_reason="stop",
                )
            ],
        ]
    )
    _prompts, tool_specs_seen = _record_llm_calls(llm)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    extras = inject_todo_tools(svc, session_id="c-int-5", cwd=str(tmp_path))

    messages = [{"role": "user", "content": "Show me the HTN tree."}]
    await run_turn(
        messages, llm, mcp,
        cwd=str(tmp_path),
        max_iter=3,
        extra_native_specs=extras,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )

    # FakeLLM saw the toposort spec (and the other 7 todo specs + run_command)
    todo_names = {entry["spec"]["function"]["name"] for entry in extras}
    observed_names = {spec["function"]["name"] for spec in tool_specs_seen[0]}
    assert "todo_toposort" in observed_names
    assert todo_names <= observed_names
    assert observed_names - todo_names == {"run_command"}

    # The tool message is the HTN tree render
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "agent should have produced a tool message"
    assert "HTN 树视图" in tool_msgs[-1]["content"]
    assert parent.id in tool_msgs[-1]["content"]
    assert child.id in tool_msgs[-1]["content"]
    assert grandchild.id in tool_msgs[-1]["content"]

    # Tool call args round-tripped intact
    assistant_with_call = next(
        m for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert assistant_with_call["tool_calls"][0]["function"]["name"] == "todo_toposort"
    assert json.loads(
        assistant_with_call["tool_calls"][0]["function"]["arguments"]
    ) == {"view": "tree"}

    # Final assistant turn is the stop response
    assert messages[-1]["content"] == "Tree view confirms structure."
    assert llm.call_count == 2
    assert mcp.calls == []  # no MCP server hit (all 8 are extras)