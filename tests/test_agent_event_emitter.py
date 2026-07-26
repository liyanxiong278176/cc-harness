"""event_emitter 形参收 4 类事件:thought / action / observation / result。

REPL 路径(event_emitter=None)零调用,既有 ~1430 测试已覆盖,本文件只测
非 None 路径以验证 4 类事件触发。

设计要点:
- 事件 schema 严格按 task-3 brief:`{"type","text","ts","iteration"}` /
  `{"type","name","args","ts","iteration"}` /
  `{"type","text","is_error","duration_ms","iteration"}` /
  `{"type","text","ts"}`。
- 暂不引入 pydantic(Task 4 落),由测试断言 schema 字段。
- ts 用 `time.time()` float,不要求单调(strict ascending)。
- observation 用 `time.time()` 计算 duration_ms,允许 0(同步/错误路径)。
"""
import json
import pytest

from tests.test_agent import FakeLLM, FakeStreamEvent, FakeMCP
from cc_harness.llm import PendingToolCall


async def _make_emitter():
    """构造 emit 函数,所有事件收进 `events` list。"""
    events = []

    async def emit(ev):
        events.append(ev)

    return events, emit


# ---------- 主测试:完整 ReAct 收到 4 类事件 ----------


@pytest.mark.asyncio
async def test_emitter_receives_thought_action_observation_result(tmp_path):
    """FakeLLM 单 tool_call + 最终回复,emitter 收到完整 4 类。

    Schema 校验按 task-3 brief,字段名/类型逐一锁定。
    """
    from cc_harness.agent import run_turn
    from cc_harness.mcp_client import ToolResult
    from cc_harness.policy import PolicyEngine

    # 起一个 file read tool(走 MCP path,allow 决策不卡 confirm)
    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"}}},
    }}
    inside = tmp_path / "hello.py"
    inside.write_text("hi", encoding="utf-8")
    pending = [PendingToolCall(
        index=0, id="c1", name="mcp__fs__read",
        arguments_json=json.dumps({"path": str(inside)}),
    )]

    # iter 1: emit thought("我先读文件") + tool_call (finish=tool_calls)
    # iter 2: emit result(" hello world") (finish=stop)
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="我先读文件",
                         pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content=" hello world",
                         pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("file contents")},
        calls=[],
    )

    events, emit = await _make_emitter()
    messages = [{"role": "user", "content": "读 hello.py"}]

    await run_turn(
        messages, llm, mcp,
        mode="coding",
        cwd=str(tmp_path),
        max_iter=5,
        policy=PolicyEngine(project_root=tmp_path),
        event_emitter=emit,
    )

    types = [e.get("type") for e in events]
    assert "thought" in types, f"缺 thought 事件: {events}"
    assert "action" in types, f"缺 action 事件: {events}"
    assert "observation" in types, f"缺 observation 事件: {events}"
    assert "result" in types, f"缺 result 事件: {events}"

    # 按类型分别 schema 校验
    for ev in events:
        t = ev["type"]
        if t == "thought":
            assert set(ev.keys()) >= {"type", "text", "ts", "iteration"}, \
                f"thought schema 不全: keys={sorted(ev.keys())}"
            assert isinstance(ev["text"], str)
            assert isinstance(ev["ts"], float)
            assert isinstance(ev["iteration"], int)
        elif t == "action":
            assert set(ev.keys()) >= {"type", "name", "args", "ts", "iteration"}, \
                f"action schema 不全: keys={sorted(ev.keys())}"
            assert isinstance(ev["name"], str)
            assert isinstance(ev["args"], dict)
            assert isinstance(ev["ts"], float)
            assert isinstance(ev["iteration"], int)
        elif t == "observation":
            assert set(ev.keys()) >= {"type", "text", "is_error",
                                      "duration_ms", "iteration"}, \
                f"observation schema 不全: keys={sorted(ev.keys())}"
            assert isinstance(ev["text"], str)
            assert isinstance(ev["is_error"], bool)
            assert isinstance(ev["duration_ms"], int)
            assert isinstance(ev["iteration"], int)
            assert ev["duration_ms"] >= 0
        elif t == "result":
            assert set(ev.keys()) >= {"type", "text", "ts"}, \
                f"result schema 不全: keys={sorted(ev.keys())}"
            assert isinstance(ev["text"], str)
            assert isinstance(ev["ts"], float)

    # result 的 text 应含 LLM 最终回复
    result_evs = [e for e in events if e["type"] == "result"]
    assert any("hello world" in e["text"] for e in result_evs), \
        f"result.text 应含 LLM 最终回复,实际: {[e['text'] for e in result_evs]}"

    # action 的 args 应包含 path
    action_evs = [e for e in events if e["type"] == "action"]
    assert any("path" in e["args"] for e in action_evs), \
        f"action.args 应含 path 字段: {[e['args'] for e in action_evs]}"

    # thought 应非空(LLM 给了文本)
    thought_evs = [e for e in events if e["type"] == "thought"]
    assert any(e["text"] for e in thought_evs), \
        "至少有一个 thought.text 非空"

    # observation 应非 error(文件读成功)
    obs_evs = [e for e in events if e["type"] == "observation"]
    assert any(not e["is_error"] for e in obs_evs), \
        "至少有一个 observation.is_error=False(成功路径)"


# ---------- emit=None 的兜底证明 ----------


@pytest.mark.asyncio
async def test_emitter_none_path_no_crash(tmp_path):
    """event_emitter=None → run_turn 正常结束,所有既有测试已隐式覆盖。

    这里再加一遍显式 sample,确保 None 路径不被意外改动。
    """
    from cc_harness.agent import run_turn
    from cc_harness.mcp_client import ToolResult
    from cc_harness.policy import PolicyEngine

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object"},
    }}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read",
                                arguments_json="{}")]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="ok",
                         pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done",
                         pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("ok")},
        calls=[],
    )
    messages = [{"role": "user", "content": "x"}]
    # 不传 event_emitter(默认 None),确保不抛
    stats = await run_turn(
        messages, llm, mcp,
        mode="coding",
        cwd=str(tmp_path),
        max_iter=5,
        policy=PolicyEngine(project_root=tmp_path),
        # event_emitter omitted → defaults None
    )
    assert stats is not None


# ---------- 错误路径 observation:is_error=True + duration_ms 仍为 int ----------


@pytest.mark.asyncio
async def test_observation_is_error_true_on_denial(tmp_path, monkeypatch):
    """ask → 用户拒 → observation 事件 is_error=True 且仍含完整 schema。

    覆盖"非主流 dispatch 路径"也 emit 一次,佐证 select-one-place-only 时
    不能漏掉 denial / error / parse-fail 路径。
    """
    from cc_harness.agent import run_turn
    from cc_harness.policy import PolicyEngine

    pending = [PendingToolCall(index=0, id="c1", name="run_command",
                                arguments_json=json.dumps(
                                    {"command": "cat ~/.ssh/id_rsa"}))]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="",
                         pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="拒绝",
                         pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # default no

    events, emit = await _make_emitter()
    messages = [{"role": "user", "content": "读密钥"}]
    await run_turn(
        messages, llm, mcp, mode="coding",
        cwd=str(tmp_path), max_iter=5,
        policy=PolicyEngine(project_root=tmp_path),
        event_emitter=emit,
    )
    obs_evs = [e for e in events if e["type"] == "observation"]
    assert obs_evs, f"denied 路径必须 emit observation;实际 events={events}"
    # 至少一个 is_error=True(L4 ask denied)
    assert any(e["is_error"] is True for e in obs_evs), \
        f"denied 路径应 emit observation.is_error=True: {obs_evs}"
    # schema 仍完整
    for e in obs_evs:
        assert isinstance(e["duration_ms"], int)
        assert e["duration_ms"] >= 0
        assert isinstance(e["text"], str)
