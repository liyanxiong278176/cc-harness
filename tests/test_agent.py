"""Tests for the ReAct loop in cc_harness.agent.run_turn.

Each test pre-programs a FakeLLM with a list of stream-event lists (one per
LLM turn) and a FakeMCP with pre-loaded tool results, then runs run_turn and
inspects the mutated `messages` list.
"""
import pytest
from dataclasses import dataclass, field
from typing import Any
from cc_harness.llm import PendingToolCall

# --- Test fixtures ---

@dataclass
class FakeMCP:
    """MCPClient replacement for tests. Pre-programmed tool results."""
    tools_spec: list[dict]
    results: dict[str, Any]  # namespaced_name -> ToolResult
    calls: list[tuple[str, dict]]

    def list_tools(self) -> list[dict]:
        return list(self.tools_spec)

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        return self.results[name]

@dataclass
class FakeStreamEvent:
    kind: str
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)  # mutable default needs factory
    content: str = ""

@dataclass
class FakeLLM:
    """Returns pre-programmed lists of StreamEvents on chat()."""
    responses: list  # list of list[StreamEvent] — one per turn
    call_count: int = 0
    model: str = "fake"

    async def chat(self, messages, tools):
        idx = self.call_count
        self.call_count += 1
        for ev in self.responses[idx]:
            yield ev

# --- Routing tests ---

@pytest.mark.asyncio
async def test_routes_normal_tool_call_executes_and_backfills(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {
        "type": "function", "function": {
            "name": "mcp__fs__read", "description": "r",
            "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
        }
    }
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json='{"p":"a.py"}')]
    events = [
        FakeStreamEvent(kind="content", text="reading "),
        FakeStreamEvent(kind="content", text="file"),
        FakeStreamEvent(kind="done", content="reading file", pending=pending, finish_reason="tool_calls"),
    ]
    llm = FakeLLM(responses=[events, [
        FakeStreamEvent(kind="content", text="summary"),
        FakeStreamEvent(kind="done", content="summary", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("file contents")},
        calls=[],
    )
    # Don't actually prompt for confirmation
    monkeypatch.setattr(agent_mod, "confirm", lambda prompt: True)

    messages = [{"role": "user", "content": "read a.py"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)

    # Expect: user, assistant(tool_call), tool, assistant(summary)
    assert len(messages) == 4
    assert messages[1]["role"] == "assistant"
    assert "tool_calls" in messages[1]
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c1"
    assert "file contents" in messages[2]["content"]
    assert messages[3]["role"] == "assistant"
    assert messages[3]["content"] == "summary"
    assert mcp.calls == [("mcp__fs__read", {"p": "a.py"})]


@pytest.mark.asyncio
async def test_routes_final_answer_when_no_tool_calls(monkeypatch):
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="answer is 42"),
        FakeStreamEvent(kind="done", content="answer is 42", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "what is 6*7?"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert len(messages) == 2
    assert messages[1] == {"role": "assistant", "content": "answer is 42"}


@pytest.mark.asyncio
async def test_routes_empty_turn_yellow_warn(monkeypatch, capfd):
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # No new assistant message added
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_finish_reason_tool_calls_with_empty_pending_degrades_to_stop(monkeypatch, capfd):
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="hi"),
        FakeStreamEvent(kind="done", content="hi", pending=[], finish_reason="tool_calls"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # Treated as final answer (not empty)
    assert len(messages) == 2
    assert messages[1] == {"role": "assistant", "content": "hi"}


@pytest.mark.asyncio
async def test_pending_tool_call_name_missing_backfills_error(monkeypatch, capfd):
    from cc_harness import agent as agent_mod

    pending = [PendingToolCall(index=0, id=None, name=None, arguments_json='{}')]
    llm = FakeLLM(responses=[
        # First turn: name-missing tool call
        [FakeStreamEvent(kind="done", content="", pending=pending, finish_reason="tool_calls")],
        # Second turn: stop with content
        [FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # Expect: user, assistant(tool_call), tool(error), assistant(ok)
    assert len(messages) == 4
    assert messages[1]["tool_calls"][0]["function"]["name"] == ""
    assert "unknown_0" in messages[2]["tool_call_id"]
    assert messages[2]["content"].startswith("[Tool Error]")


@pytest.mark.asyncio
async def test_max_iter_reached_with_pending_drops_tool_calls(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object"},
    }}
    # Always returns a tool call — drives the loop to max_iter
    responses = []
    for i in range(25):
        pending = [PendingToolCall(index=0, id=f"c{i}", name="mcp__fs__read", arguments_json="{}")]
        responses.append([
            FakeStreamEvent(kind="done", content=f"thought {len(responses)}",
                            pending=pending, finish_reason="tool_calls"),
        ])
    llm = FakeLLM(responses=responses)
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"mcp__fs__read": ToolResult.success("x")},
                  calls=[])
    monkeypatch.setattr(agent_mod, "confirm", lambda prompt: True)

    messages = [{"role": "user", "content": "loop"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=20)
    # Spec: on iter==20 with has_tool_calls=True, the agent MUST:
    #   (1) drop pending tool_calls (no tool_calls on the final assistant message)
    #   (2) NOT append any role:tool backfill after the final assistant
    #   (3) emit a gentle fallback text instead
    final = messages[-1]
    assert final["role"] == "assistant"
    assert "tool_calls" not in final, "final assistant must not have tool_calls"
    assert final["content"]  # either a thought or the fallback text

    # Walk backwards: find the LAST assistant message; nothing after it should be role:tool
    final_assistant_idx = max(
        i for i, m in enumerate(messages) if m["role"] == "assistant"
    )
    assert not any(
        m["role"] == "tool" for m in messages[final_assistant_idx + 1:]
    ), "no role:tool backfill after the final assistant message"

    # The total number of assistant-with-tool_calls messages should be < 20
    # (one fewer than max_iter because the final turn drops them)
    tool_call_msgs = [m for m in messages if m.get("role") == "assistant" and "tool_calls" in m]
    assert len(tool_call_msgs) < 20


@pytest.mark.asyncio
async def test_danger_command_user_says_no_llm_changes_tool(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    bash_tool = {"type": "function", "function": {
        "name": "mcp__bash__run", "description": "b",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    }}
    safe_tool = {"type": "function", "function": {
        "name": "mcp__safe__read", "description": "s",
        "parameters": {"type": "object"},
    }}
    # Turn 1: LLM tries to call bash with rm -rf; user says N.
    # Turn 2: LLM tries safe tool; executes.
    pending1 = [PendingToolCall(index=0, id="c1", name="mcp__bash__run",
                                arguments_json='{"command":"rm -rf /tmp/x"}')]
    pending2 = [PendingToolCall(index=0, id="c2", name="mcp__safe__read", arguments_json="{}")]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="", pending=pending1, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="", pending=pending2, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(
        tools_spec=[bash_tool, safe_tool],
        results={"mcp__safe__read": ToolResult.success("ok")},
        calls=[],
    )
    confirm_calls: list[str] = []
    def fake_confirm(prompt: str) -> bool:
        confirm_calls.append(prompt)
        return False  # user rejects rm -rf
    monkeypatch.setattr(agent_mod, "confirm", fake_confirm)

    messages = [{"role": "user", "content": "clean up"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert confirm_calls == ["Confirm execution?"]
    # Bash tool was NOT called
    assert all(name != "mcp__bash__run" for name, _ in mcp.calls)
    # Safe tool WAS called
    assert ("mcp__safe__read", {}) in mcp.calls


# --- Mode branches (task #4) ---

@pytest.mark.asyncio
async def test_plan_mode_does_not_execute_tools(capfd):
    """In plan mode, no tools are executed even if a tool_call comes through."""
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object"},
    }}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json="{}")]
    llm = FakeLLM(responses=[[
        # Even though finish_reason=tool_calls, in plan mode the call is
        # dropped and the content is treated as the final answer.
        FakeStreamEvent(kind="done", content="## 目标\n完成 X", pending=pending, finish_reason="tool_calls"),
    ]])
    mcp = FakeMCP(tools_spec=[fs_tool], results={}, calls=[])

    messages = [{"role": "user", "content": "plan X"}]
    await agent_mod.run_turn(messages, llm, mcp, mode="plan", max_iter=5)

    # Tool was NOT called
    assert mcp.calls == []
    # Final assistant message is the plan content
    assert messages[-1] == {"role": "assistant", "content": "## 目标\n完成 X"}


@pytest.mark.asyncio
async def test_plan_mode_refreshes_system_prompt():
    """When cwd is provided, the system prompt is set to the plan-mode variant."""
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="plan", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, mode="plan", cwd="/test/cwd")

    assert messages[0]["role"] == "system"
    assert "Plan 模式" in messages[0]["content"]
    assert "/test/cwd" in messages[0]["content"]


@pytest.mark.asyncio
async def test_coding_mode_does_not_inject_plan_override():
    """In coding mode, the plan-mode override is NOT in the system prompt."""
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, mode="coding", cwd="/test/cwd")

    assert messages[0]["role"] == "system"
    assert "Plan 模式" not in messages[0]["content"]
    assert "Design 模式" not in messages[0]["content"]
    # Coding sections ARE present
    assert "工具使用纪律" in messages[0]["content"]


@pytest.mark.asyncio
async def test_design_mode_saves_output_to_disk(tmp_path):
    """Design mode persists the final assistant content under design_dir."""
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="mermaid\ngraph TD;\nA-->B", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "draw graph"}]
    await agent_mod.run_turn(
        messages, llm, mcp, mode="design",
        cwd="/x", design_dir=tmp_path,
    )

    # File created under tmp_path with .md suffix
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "mermaid" in saved
    assert "A-->B" in saved
    # Slug derived from first line "mermaid" — file name should contain it
    assert "mermaid" in saved or "mermaid" in files[0].name


@pytest.mark.asyncio
async def test_plan_mode_does_not_save_to_disk(tmp_path):
    """Plan mode prints the plan but does NOT save to disk."""
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="## 目标\nX", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "plan"}]
    await agent_mod.run_turn(
        messages, llm, mcp, mode="plan",
        cwd="/x", design_dir=tmp_path,
    )
    assert list(tmp_path.glob("*.md")) == []


@pytest.mark.asyncio
async def test_unknown_mode_raises():
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    with pytest.raises(ValueError, match="unknown mode"):
        await agent_mod.run_turn(messages, llm, mcp, mode="bogus")


@pytest.mark.asyncio
async def test_cwd_none_leaves_messages_unchanged():
    """If cwd is None, run_turn does not touch messages[0] (callers manage prompt)."""
    from cc_harness import agent as agent_mod
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, mode="coding", cwd=None)
    # No system prompt was inserted
    assert messages[0]["role"] == "user"


# --- _save_design_output + _refresh_system_prompt helpers ---

def test_save_design_output_writes_file(tmp_path):
    from cc_harness.agent import _save_design_output
    messages = [
        {"role": "user", "content": "draw"},
        {"role": "assistant", "content": "mermaid\nA-->B"},
    ]
    path = _save_design_output(messages, base_dir=tmp_path)
    assert path is not None
    assert path.exists()
    assert path.suffix == ".md"
    assert "A-->B" in path.read_text(encoding="utf-8")


def test_save_design_output_no_assistant_returns_none(tmp_path):
    from cc_harness.agent import _save_design_output
    messages = [{"role": "user", "content": "x"}]
    assert _save_design_output(messages, base_dir=tmp_path) is None


def test_save_design_output_creates_dir(tmp_path):
    from cc_harness.agent import _save_design_output
    target = tmp_path / "nested" / "designs"
    messages = [{"role": "assistant", "content": "x"}]
    path = _save_design_output(messages, base_dir=target)
    assert path is not None
    assert target.exists()


def test_refresh_system_prompt_inserts_when_missing():
    from cc_harness.agent import _refresh_system_prompt
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, cwd="/abc", mode="plan")
    assert messages[0]["role"] == "system"
    assert "/abc" in messages[0]["content"]
    assert "Plan 模式" in messages[0]["content"]


def test_refresh_system_prompt_updates_existing():
    from cc_harness.agent import _refresh_system_prompt
    messages = [
        {"role": "system", "content": "old prompt"},
        {"role": "user", "content": "x"},
    ]
    _refresh_system_prompt(messages, cwd="/new", mode="design")
    # System message updated, not duplicated
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] != "old prompt"
    assert "Design 模式" in messages[0]["content"]
