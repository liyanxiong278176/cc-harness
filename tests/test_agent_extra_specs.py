"""Contract tests for `extra_native_specs` parameter on cc_harness.agent.run_turn.

run_turn accepts an optional `extra_native_specs: list[dict]` so the locomo
runner can inject memory tools (memory_recall / memory_save) alongside the
existing NATIVE_TOOLS. Each entry: {"spec": <OpenAI tool spec>, "handler":
async callable, "deps": <dict passed through to handler as kwargs>}.

Two contracts:
- Pass `None` (default): REPL behavior unchanged.
- Pass a list with one entry: the LLM sees the extra spec AND tool_calls to
  that name dispatch to the entry's handler with (args, cwd, **deps).
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cc_harness import agent as agent_mod
from cc_harness.llm import PendingToolCall
from cc_harness.mcp_client import ToolResult
from cc_harness.tokens import TurnTokenStats


# --- Inline fakes (mirror tests/test_agent.py so the new file is self-contained) ---

@dataclass
class _FakeMCP:
    tools_spec: list[dict]
    results: dict
    calls: list = field(default_factory=list)

    def list_tools(self) -> list[dict]:
        return list(self.tools_spec)

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        return self.results[name]


@dataclass
class _FakeStreamEvent:
    kind: str
    text: str = ""
    finish_reason: str | None = None
    pending: list = field(default_factory=list)
    content: str = ""
    usage: object = None  # cc_harness._stream_one_turn reads ev.usage


@dataclass
class _FakeLLM:
    responses: list  # list of list[_FakeStreamEvent] — one list per turn

    async def chat(self, messages, tools):
        idx = getattr(self, "_call_count", 0)
        self._call_count = idx + 1
        for ev in self.responses[idx]:
            yield ev


# --- Contract test 1: extra_native_specs=None → unchanged behavior ---

@pytest.mark.asyncio
async def test_extra_specs_none_unchanged_behavior():
    """`extra_native_specs=None` (default) must NOT change run_turn behavior.

    Verifies the parameter exists, is accepted, and a normal coding turn still
    returns a TurnTokenStats — no tool dispatch path is touched.
    """
    llm = _FakeLLM(responses=[[
        _FakeStreamEvent(kind="content", text="hi back"),
        _FakeStreamEvent(kind="done", content="hi back", pending=[], finish_reason="stop"),
    ]])
    mcp = _FakeMCP(tools_spec=[], results={})
    messages = [{"role": "user", "content": "hi"}]

    stats = await agent_mod.run_turn(
        messages, llm, mcp, max_iter=3, mode="coding",
        cwd=str(Path(os.getcwd()).resolve()),
    )
    assert isinstance(stats, TurnTokenStats), \
        "default cwd turn must return TurnTokenStats (extra_native_specs not passed)"


# --- Contract test 2: extra_native_specs dispatches to handler with deps ---

@pytest.mark.asyncio
async def test_extra_specs_dispatched_to_handler(monkeypatch):
    """Pass extras: handler is called with args (positional), cwd (kwarg),
    and every key from deps (kwarg).

    Tool "my_tool" classifies as "unknown" → policy asks for confirmation →
    monkeypatch `confirm_tool` to "yes" so the dispatch path runs through.
    """
    # ToolResult-returning handler; we then assert the dispatcher reached it.
    handler = AsyncMock(return_value=ToolResult.success("handler-output"))

    handler_spec = {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "test",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
        },
    }
    extras = [{
        "spec": handler_spec,
        "handler": handler,
        "deps": {"retriever": "fake-retriever"},
    }]

    # LLM: turn 1 emits the custom tool call; turn 2 stops with final content.
    pending = [PendingToolCall(
        index=0, id="c1", name="my_tool",
        arguments_json='{"x":"hello"}',
    )]
    llm = _FakeLLM(responses=[
        [
            _FakeStreamEvent(kind="content", text="ok"),
            _FakeStreamEvent(
                kind="done", content="ok",
                pending=pending, finish_reason="tool_calls",
            ),
        ],
        [
            _FakeStreamEvent(kind="content", text="done"),
            _FakeStreamEvent(
                kind="done", content="done",
                pending=[], finish_reason="stop",
            ),
        ],
    ])
    mcp = _FakeMCP(tools_spec=[], results={})
    # Bypass L4 "unknown tool" gate so the dispatch reaches the handler.
    monkeypatch.setattr(agent_mod, "confirm_tool", lambda *a, **k: "yes")

    cwd = str(Path(os.getcwd()).resolve())
    messages = [{"role": "user", "content": "test"}]
    await agent_mod.run_turn(
        messages, llm, mcp, max_iter=3, mode="coding", cwd=cwd,
        extra_native_specs=extras,
    )

    assert handler.await_count >= 1, "extra tool handler must have been awaited"
    call = handler.await_args
    assert call is not None
    # args is positional
    assert call.args and call.args[0] == {"x": "hello"}, \
        f"handler args must be the parsed JSON dict, got {call.args!r}"
    # cwd is passed as a kwarg (resolved project_root)
    assert call.kwargs.get("cwd") == cwd, \
        f"cwd must equal resolved project_root ({cwd!r}), got {call.kwargs.get('cwd')!r}"
    # deps are merged into kwargs
    assert call.kwargs.get("retriever") == "fake-retriever", \
        f"deps must be passed through as kwargs, got {call.kwargs!r}"


# --- Phase 1 Q1 uplift: qa_context kwarg injects qa_intro into system prompt ---

@pytest.mark.asyncio
async def test_qa_context_injects_qa_intro_into_system_prompt(monkeypatch):
    """传 qa_context={"q_type": N} → run_turn 重渲 system 段含 qa_intro + cat=N。"""
    from cc_harness import agent as agent_mod
    llm = _FakeLLM(responses=[[
        _FakeStreamEvent(kind="content", text="hi"),
        _FakeStreamEvent(kind="done", content="hi", pending=[], finish_reason="stop"),
    ]])
    mcp = _FakeMCP(tools_spec=[], results={})
    monkeypatch.setattr(agent_mod, "confirm_tool", lambda *a, **k: "yes")
    cwd = str(Path(os.getcwd()).resolve())
    messages = [{"role": "user", "content": "Q: test?"}]
    await agent_mod.run_turn(
        messages, llm, mcp, max_iter=1, mode="chat", cwd=cwd,
        qa_context={"q_type": 2, "must_answer": True},
    )
    sys = messages[0]
    assert sys.get("role") == "system"
    assert "当前问题类型:QA" in sys["content"]
    assert "cat=2" in sys["content"]
    # 必须答规则
    assert "必须给出具体答案" in sys["content"]


@pytest.mark.asyncio
async def test_qa_context_none_keeps_legacy_prompt(monkeypatch):
    """qa_context=None → 不渲染 qa_intro(向后兼容,test_agent.py 等不传 qa_context 不受影响)。"""
    from cc_harness import agent as agent_mod
    llm = _FakeLLM(responses=[[
        _FakeStreamEvent(kind="content", text="ok"),
        _FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop"),
    ]])
    mcp = _FakeMCP(tools_spec=[], results={})
    monkeypatch.setattr(agent_mod, "confirm_tool", lambda *a, **k: "yes")
    cwd = str(Path(os.getcwd()).resolve())
    messages = [{"role": "user", "content": "hi"}]
    await agent_mod.run_turn(
        messages, llm, mcp, max_iter=1, mode="chat", cwd=cwd,
        # 不传 qa_context
    )
    sys = messages[0]
    assert "当前问题类型" not in sys["content"]

