from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cc_harness.llm import PendingToolCall
from cc_harness.loop_control import CompletionContract, LoopControlConfig, RecoveryPolicy
from cc_harness.mcp_client import ToolResult


@dataclass
class Event:
    kind: str
    text: str = ""
    content: str = ""
    pending: list[Any] | None = None
    finish_reason: str | None = None
    usage: Any = None


class FakeLLM:
    def __init__(self, responses: list[list[Event]]) -> None:
        self.responses = responses
        self.call_count = 0
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages, tools):
        self.seen_messages.append([dict(message) for message in messages])
        response = self.responses[self.call_count]
        self.call_count += 1
        for event in response:
            yield event


class EmptyMCP:
    async def list_tools(self):
        return []

    async def call_tool(self, name, args):
        raise AssertionError(f"unexpected MCP call: {name}")


def tool_turn(name: str, args: dict, call_id: str) -> list[Event]:
    return [Event(
        kind="done",
        pending=[PendingToolCall(
            index=0,
            id=call_id,
            name=name,
            arguments_json=json.dumps(args),
        )],
        finish_reason="tool_calls",
    )]


def final_turn(text: str = "done") -> list[Event]:
    return [Event(kind="done", content=text, pending=[], finish_reason="stop")]


@pytest.mark.asyncio
async def test_completion_verifier_rejects_candidate_and_requests_verification(
    tmp_path: Path, monkeypatch,
) -> None:
    from cc_harness import agent as agent_mod

    async def write_handler(args, *, cwd):
        target = Path(cwd) / args["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"], encoding="utf-8")
        return ToolResult.success("written")

    async def command_handler(args, *, cwd):
        return ToolResult.success("1 passed")

    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["Write"], "handler", write_handler)
    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["run_command"], "handler", command_handler)
    llm = FakeLLM([
        tool_turn("Write", {"path": "app.py", "content": "x = 1", "mode": "create_only"}, "w1"),
        final_turn("implemented"),
        tool_turn("run_command", {"command": "python -m pytest"}, "t1"),
        final_turn("implemented and tested"),
    ])
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    async def confirm(_name, _args, _reason):
        return "yes"

    messages = [{"role": "user", "content": "create app.py"}]
    stats = await agent_mod.run_turn(
        messages,
        llm,
        EmptyMCP(),
        cwd=str(tmp_path),
        session_id="completion-test",
        event_emitter=emit,
        confirm_handler=confirm,
        max_iter=6,
        loop_control_config=LoopControlConfig(),
    )

    assert stats.error is None
    assert llm.call_count == 4, (events, stats.tool_call_log, messages)
    assert any(event["type"] == "completion_rejected" for event in events)
    assert any(
        message.get("role") == "user" and "completion_verification" in message.get("content", "")
        for message in messages
    )
    assert messages[-1]["content"] == "implemented and tested"


@pytest.mark.asyncio
async def test_transient_tool_error_is_retried_without_an_extra_model_call(
    tmp_path: Path, monkeypatch,
) -> None:
    from cc_harness import agent as agent_mod

    attempts = 0

    async def flaky_read(args, *, cwd):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ToolResult.error("unavailable", "HTTP 503 service unavailable")
        return ToolResult.success("contents")

    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["Read"], "handler", flaky_read)
    llm = FakeLLM([tool_turn("Read", {"path": "a.py"}, "r1"), final_turn()])
    messages = [{"role": "user", "content": "read a.py"}]
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    await agent_mod.run_turn(
        messages,
        llm,
        EmptyMCP(),
        cwd=str(tmp_path),
        session_id="retry-test",
        event_emitter=emit,
        max_iter=4,
        loop_control_config=LoopControlConfig(
            completion_contract=CompletionContract(
                require_verification_after_code_changes=False,
            ),
            recovery_policy=RecoveryPolicy(max_transient_retries=1, retry_delay_seconds=0),
        ),
    )
    assert attempts == 2
    assert llm.call_count == 2
    assert any(event["type"] == "retrying" for event in events)
    tool_message = next(message for message in messages if message.get("role") == "tool")
    assert "contents" in tool_message["content"]


@pytest.mark.asyncio
async def test_parallel_native_reads_are_concurrent_and_results_stay_ordered(
    tmp_path: Path, monkeypatch,
) -> None:
    from cc_harness import agent as agent_mod

    active = 0
    peak = 0

    async def read_handler(args, *, cwd):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return ToolResult.success(args["path"])

    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["Read"], "handler", read_handler)
    pending = [
        PendingToolCall(index=0, id="r1", name="Read", arguments_json='{"path":"a.py"}'),
        PendingToolCall(index=1, id="r2", name="Read", arguments_json='{"path":"b.py"}'),
    ]
    llm = FakeLLM([
        [Event(kind="done", pending=pending, finish_reason="tool_calls")],
        final_turn(),
    ])
    messages = [{"role": "user", "content": "read both"}]
    await agent_mod.run_turn(
        messages,
        llm,
        EmptyMCP(),
        cwd=str(tmp_path),
        session_id="parallel-test",
        max_iter=4,
        loop_control_config=LoopControlConfig(),
    )
    assert peak == 2
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["r1", "r2"]


@pytest.mark.asyncio
async def test_repeated_identical_trajectory_emits_stall_feedback(
    tmp_path: Path, monkeypatch,
) -> None:
    from cc_harness import agent as agent_mod

    calls = 0

    async def read_handler(args, *, cwd):
        nonlocal calls
        calls += 1
        return ToolResult.success("unchanged")

    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["Read"], "handler", read_handler)
    llm = FakeLLM([
        tool_turn("Read", {"path": "a.py"}, "r1"),
        tool_turn("Read", {"path": "a.py"}, "r2"),
        tool_turn("Read", {"path": "a.py"}, "r3"),
        tool_turn("Read", {"path": "a.py"}, "r4"),
        final_turn(),
    ])
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    await agent_mod.run_turn(
        [{"role": "user", "content": "inspect"}],
        llm,
        EmptyMCP(),
        cwd=str(tmp_path),
        session_id="stall-test",
        event_emitter=emit,
        max_iter=6,
        loop_control_config=LoopControlConfig(stall_repeat_threshold=3),
    )
    assert any(event["type"] == "loop_stall" for event in events)
    assert any(event["type"] == "loop_stall_blocked" for event in events)
    assert calls == 3


@pytest.mark.asyncio
async def test_run_turn_journal_contains_finished_action_and_recoverable_state(
    tmp_path: Path, monkeypatch,
) -> None:
    from cc_harness import agent as agent_mod

    async def read_handler(args, *, cwd):
        return ToolResult.success("ok")

    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["Read"], "handler", read_handler)
    llm = FakeLLM([tool_turn("Read", {"path": "a.py"}, "r1"), final_turn()])
    await agent_mod.run_turn(
        [{"role": "user", "content": "read"}],
        llm,
        EmptyMCP(),
        cwd=str(tmp_path),
        session_id="journal-test",
        max_iter=4,
        loop_control_config=LoopControlConfig(),
    )
    path = tmp_path / ".cc-harness" / "action-journal" / "journal-test.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["kind"] for event in events] == ["tool_started", "tool_finished"]
    assert events[-1]["state"]["read_paths"] == ["a.py"]
