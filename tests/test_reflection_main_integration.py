"""E2 反思节点 T2.2 集成测试:agent.run_turn 4 类 emit + _refresh_system_prompt 注入。

5 cases:
1. max_iter 触达 → engine.emit 收到 max_iter_reached 事件
2. empty_turn 触达(重试一次后还空) → engine.emit 收到 empty_turn_loop 事件
3. tool is_error 连续 2+ → engine.emit 收到 tool_error_burst 事件
4. 同 tool + 同 args 调 2+ 次 → engine.emit 收到 tool_retry_burst 事件
5. reflection_engine.get_last_neg_reflection 注入到 _refresh_system_prompt extra_ctx
   (extra_ctx["last_neg_reflection"]=非 None 时,SECTION_POOL 拼装会包 <上一轮反思>)

实施要点:
- reflection_engine 形参 = None 保持向后兼容
- emit 全部 try/except + pass 兜底
- _refresh_system_prompt 末尾 caller 拼 extra_ctx["last_neg_reflection"]
"""
import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from dataclasses import dataclass, field

from cc_harness.llm import PendingToolCall
from cc_harness.tokens import UsageRecord


# --- Reused Fake fixtures (mirror tests/test_agent.py) ---


@dataclass
class _FakeMCP:
    tools_spec: list
    results: dict
    calls: list = field(default_factory=list)

    def list_tools(self):
        return list(self.tools_spec)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.results[name]


@dataclass
class _FakeEv:
    kind: str
    text: str = ""
    tool_call: "PendingToolCall | None" = None
    finish_reason: str | None = None
    pending: list = field(default_factory=list)
    content: str = ""
    usage: "UsageRecord | None" = None


@dataclass
class _FakeLLM:
    """Replays pre-programmed stream events; raises if responses run out."""
    responses: list
    call_count: int = 0

    async def chat(self, messages, tools):
        idx = self.call_count
        self.call_count += 1
        for ev in self.responses[idx]:
            yield ev


def _make_forever_tool_llm(name="mcp__fs__read", args=None, n=10):
    """LLM 永远 finish_reason=tool_calls + 同 name+args → 必触达 max_iter。"""
    args = args or {"path": "/x"}
    p = [PendingToolCall(index=0, id="t1", name=name,
                          arguments_json=json.dumps(args))]
    return _FakeLLM(responses=[
        [_FakeEv(kind="done", content="", pending=p, finish_reason="tool_calls")]
        for _ in range(n)
    ])


def _make_forever_tool_llm_in_workspace(ws_dir, name="mcp__fs__read", n=10):
    """LLM 永远 tool_calls,但 path 在 workspace 内 → policy allow(不需 confirm)。"""
    inside = ws_dir / "x.txt"
    inside.write_text("x", encoding="utf-8")
    p = [PendingToolCall(index=0, id="t1", name=name,
                          arguments_json=json.dumps({"path": str(inside)}))]
    return _FakeLLM(responses=[
        [_FakeEv(kind="done", content="", pending=p, finish_reason="tool_calls")]
        for _ in range(n)
    ])


def _make_reflection_engine():
    re_emit = MagicMock()
    re_emit.emit = AsyncMock()
    re_emit.get_last_neg_reflection = MagicMock(return_value=None)
    re_emit.drain = AsyncMock()  # not strictly required, but safe
    return re_emit


# --- Tests ---


@pytest.mark.asyncio
async def test_max_iter_emit_triggers_neg_reflection(tmp_path):
    """max_iter 触达时,ReflectionEngine.emit 收到 max_iter_reached 事件。"""
    from cc_harness import agent as agent_mod
    from cc_harness.policy import PolicyEngine
    from cc_harness.mcp_client import ToolResult

    fs_tool = {
        "type": "function", "function": {
            "name": "mcp__fs__read", "description": "r",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    }
    llm = _make_forever_tool_llm_in_workspace(tmp_path, n=5)
    mcp = _FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("ok")},
        calls=[],
    )
    re_emit = _make_reflection_engine()

    messages = [{"role": "user", "content": "do it"}]
    policy = PolicyEngine(project_root=tmp_path)
    # max_iter=3 → 必触达兜底;reflect engine 接 events
    await agent_mod.run_turn(
        messages=messages, llm=llm, mcp=mcp, mode="coding",
        max_iter=3, cwd=str(tmp_path), policy=policy,
        reflection_engine=re_emit,
    )
    # emit 应至少收到 1 个 max_iter_reached 事件
    max_iter_calls = [
        c for c in re_emit.emit.await_args_list
        if c.args and c.args[0].event_type == "max_iter"
    ]
    assert len(max_iter_calls) >= 1, (
        f"expected max_iter event, got: "
        f"{[c.args[0].event_type for c in re_emit.emit.await_args_list]}"
    )


@pytest.mark.asyncio
async def test_empty_turn_retry_emit_triggers_neg_reflection(tmp_path):
    """空 content 二次重试后,emit 收到 empty_turn_loop 事件。"""
    from cc_harness import agent as agent_mod

    llm = _FakeLLM(responses=[
        [_FakeEv(kind="done", content="", pending=[], finish_reason="stop")],
        [_FakeEv(kind="done", content="", pending=[], finish_reason="stop")],
    ])
    mcp = _FakeMCP(tools_spec=[], results={}, calls=[])
    re_emit = _make_reflection_engine()

    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(
        messages=messages, llm=llm, mcp=mcp, mode="coding",
        max_iter=5, reflection_engine=re_emit,
    )
    empty_calls = [
        c for c in re_emit.emit.await_args_list
        if c.args and c.args[0].event_type == "empty_turn"
    ]
    assert len(empty_calls) >= 1, (
        f"expected empty_turn event, got: "
        f"{[c.args[0].event_type for c in re_emit.emit.await_args_list]}"
    )


@pytest.mark.asyncio
async def test_tool_error_burst_emit_on_two_consecutive_errors(tmp_path):
    """tool is_error 连续 2+ 时,emit 收到 tool_error_burst。"""
    from cc_harness import agent as agent_mod
    from cc_harness.policy import PolicyEngine

    # 两次 tool_call 失败:name-missing → JSON parse 失败
    pending_bad1 = [PendingToolCall(index=0, id="c1", name=None, arguments_json="{}")]
    pending_bad2 = [PendingToolCall(index=0, id="c2", name="mcp__fs__read",
                                     arguments_json="not-json{{{")]
    llm = _FakeLLM(responses=[
        # iter 0: name-missing → is_error tool message
        [_FakeEv(kind="done", content="", pending=pending_bad1, finish_reason="tool_calls")],
        # iter 1: JSON parse 失败 → is_error tool message
        [_FakeEv(kind="done", content="", pending=pending_bad2, finish_reason="tool_calls")],
        # iter 2: 兜底
        [_FakeEv(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = _FakeMCP(tools_spec=[], results={}, calls=[])
    re_emit = _make_reflection_engine()

    messages = [{"role": "user", "content": "x"}]
    policy = PolicyEngine(project_root=tmp_path)
    await agent_mod.run_turn(
        messages=messages, llm=llm, mcp=mcp, mode="coding",
        max_iter=10, cwd=str(tmp_path), policy=policy,
        reflection_engine=re_emit,
    )
    burst_calls = [
        c for c in re_emit.emit.await_args_list
        if c.args and c.args[0].event_type == "tool_error_burst"
    ]
    assert len(burst_calls) >= 1, (
        f"expected tool_error_burst event, got: "
        f"{[c.args[0].event_type for c in re_emit.emit.await_args_list]}"
    )


@pytest.mark.asyncio
async def test_tool_retry_burst_emit_on_repeated_call(tmp_path):
    """同 tool+同 args 调 2+ 次时,emit 收到 tool_retry_burst(ambig)。"""
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult
    from cc_harness.policy import PolicyEngine

    fs_tool = {
        "type": "function", "function": {
            "name": "mcp__fs__read", "description": "r",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    }
    inside = tmp_path / "x.txt"
    inside.write_text("x", encoding="utf-8")
    pending_same = [PendingToolCall(index=0, id="c1", name="mcp__fs__read",
                                     arguments_json=json.dumps({"path": str(inside)}))]
    llm = _FakeLLM(responses=[
        [_FakeEv(kind="done", content="", pending=pending_same, finish_reason="tool_calls")],
        [_FakeEv(kind="done", content="", pending=pending_same, finish_reason="tool_calls")],
        [_FakeEv(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = _FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("ok")},
        calls=[],
    )
    re_emit = _make_reflection_engine()

    messages = [{"role": "user", "content": "x"}]
    policy = PolicyEngine(project_root=tmp_path)
    await agent_mod.run_turn(
        messages=messages, llm=llm, mcp=mcp, mode="coding",
        max_iter=10, cwd=str(tmp_path), policy=policy,
        reflection_engine=re_emit,
    )
    retry_calls = [
        c for c in re_emit.emit.await_args_list
        if c.args and c.args[0].event_type == "tool_retry_burst"
    ]
    assert len(retry_calls) >= 1, (
        f"expected tool_retry_burst event, got: "
        f"{[c.args[0].event_type for c in re_emit.emit.await_args_list]}"
    )


@pytest.mark.asyncio
async def test_refresh_system_prompt_includes_last_neg_reflection(tmp_path):
    """reflection_engine.get_last_neg_reflection() 非 None → extra_ctx 透传到
    _refresh_system_prompt,SECTION_POOL 拼装包含 <上一轮反思>。"""
    from cc_harness.agent import _refresh_system_prompt

    re_emit = _make_reflection_engine()
    re_emit.get_last_neg_reflection = MagicMock(return_value="上轮反思:测试不通过因为没用 Grep")

    messages = [{"role": "user", "content": "x"}]
    # 手动模拟 run_turn 在 _refresh 之前的 caller 拼装 extra_ctx
    extra_ctx = {}
    if re_emit is not None:
        extra_ctx["last_neg_reflection"] = re_emit.get_last_neg_reflection()
    _refresh_system_prompt(messages, str(tmp_path), "coding", extra_ctx=extra_ctx)
    assert "上轮反思" in messages[0]["content"] or "<上一轮反思>" in messages[0]["content"]
    assert "测试不通过" in messages[0]["content"]


def test_run_turn_default_signature_backward_compatible():
    """reflection_engine 形参默认 None,保证旧 caller 不破。"""
    import inspect
    from cc_harness.agent import run_turn
    sig = inspect.signature(run_turn)
    assert "reflection_engine" in sig.parameters
    assert sig.parameters["reflection_engine"].default is None
