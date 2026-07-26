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


# ---------- Fix round 1:Reviewer flagged Issues ----------

@pytest.mark.asyncio
async def test_action_with_non_dict_args_emits_error_observation(tmp_path):
    """Fix #1:reviewer flagged `action.args` may not be dict。

    OpenAI tool_calls 的 `arguments` JSON 顶层合法值含 list / null / scalar
    (e.g. `"[]"` / `"null"` / `"42"`),不是所有 schema 都会卡掉。一旦 args 非 dict,
    既有的 JSON parse + schema validate 都不会拦(validate_* 期望 dict),会让
    非 dict 流到 `_dispatch` 与 emit action 事件,违反 brief 锁定的
    `action.args: dict` schema。

    期望:agent.py 在 json.loads 后加 `isinstance(args, dict)` 检查。非 dict:
      - emit observation(is_error=True, text="tool args must be a JSON object")
      - NOT emit action 事件
      - 回填 tool message(messages 历史不能断)
      - 不进 policy / dispatch
    """
    from cc_harness.agent import run_turn
    from cc_harness.mcp_client import ToolResult
    from cc_harness.policy import PolicyEngine

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"}}},
    }}
    # 关键:arguments_json 是合法 JSON 但顶层非 object(list)
    pending = [PendingToolCall(
        index=0, id="c1", name="mcp__fs__read",
        arguments_json="[]",
    )]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="尝试调用",
                         pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="好的不调用",
                         pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("never called")},
        calls=[],
    )

    events, emit = await _make_emitter()
    messages = [{"role": "user", "content": "试一下"}]
    await run_turn(
        messages, llm, mcp,
        mode="coding",
        cwd=str(tmp_path),
        max_iter=5,
        policy=PolicyEngine(project_root=tmp_path),
        event_emitter=emit,
    )

    # 关键断言 1:不应 emit action 事件(non-dict args 跳过 dispatch)
    action_evs = [e for e in events if e["type"] == "action"]
    assert action_evs == [], \
        f"non-dict args 不应 emit action 事件;实际 actions={action_evs}"

    # 关键断言 2:必须 emit observation(is_error=True),text 提示 args 类型错误
    obs_evs = [e for e in events if e["type"] == "observation"]
    assert obs_evs, f"必须 emit observation 提示参数错误;events={events}"
    assert any(e["is_error"] is True for e in obs_evs), \
        f"必须 emit is_error=True observation;实际 obs={obs_evs}"
    err_obs = [e for e in obs_evs if e["is_error"]
               and "JSON object" in e["text"]]
    assert err_obs, (
        f"必须有一个 observation 说明 args 必须为 JSON object;实际 obs={obs_evs}"
    )

    # 关键断言 3:_dispatch 不应被调到(MCP calls 应为空)
    assert mcp.calls == [], (
        f"non-dict args 路径不应 dispatch;mcp.calls={mcp.calls}"
    )

    # 关键断言 4:messages 历史里仍有一条 tool message(role=tool, is_error=True),
    # 否则下一轮 LLM 没有 tool_call_id 对应会报 schema 错
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert any(m.get("is_error") is True for m in tool_msgs), \
        f"messages 历史应回填 is_error=True 的 tool message;messages={messages}"


@pytest.mark.asyncio
async def test_emitter_exception_does_not_break_run_turn(tmp_path, caplog):
    """Fix #2:reviewer flagged `_safe_emit` 静默吞所有异常。

    emit 异常(emitter 端 disconnect / 序列化错 / WS 层断)必须:
      - 不破主 ReAct 循环(run_turn 仍正常结束,emit `result` 事件)
      - 在 logger 留 debug 日志(便于诊断)

    fail-soft 是契约:REPL 流不能因为 UI emitter 挂了就卡死。
    """
    import logging

    from cc_harness.agent import run_turn
    from cc_harness.mcp_client import ToolResult
    from cc_harness.policy import PolicyEngine

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object"},
    }}
    inside = tmp_path / "x.py"
    inside.write_text("ok", encoding="utf-8")
    pending = [PendingToolCall(
        index=0, id="c1", name="mcp__fs__read",
        arguments_json=json.dumps({"path": str(inside)}),
    )]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="reading",
                         pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done",
                         pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("file contents")},
        calls=[],
    )

    # 关键:emitter 每次调都抛 RuntimeError(模拟 WS 断 / 序列化错)
    async def broken_emit(ev):
        raise RuntimeError("simulated WS disconnect")

    messages = [{"role": "user", "content": "读 x.py"}]
    with caplog.at_level(logging.DEBUG, logger="cc_harness.agent"):
        stats = await run_turn(
            messages, llm, mcp,
            mode="coding",
            cwd=str(tmp_path),
            max_iter=5,
            policy=PolicyEngine(project_root=tmp_path),
            event_emitter=broken_emit,
        )

    # 关键断言 1:run_turn 正常返回,不被 emitter 异常拖累
    assert stats is not None, "emitter 异常不应让 run_return 返回 None"

    # 关键断言 2:tool dispatch 实际仍跑了(emit 异常不拦主循环)
    assert mcp.calls == [("mcp__fs__read", {"path": str(inside)})], (
        f"emitter 异常不应阻止 _dispatch;mcp.calls={mcp.calls}"
    )

    # 关键断言 3:logger 收到至少一条 debug 日志(异常被记录便于诊断)
    debug_msgs = [r for r in caplog.records
                  if r.levelno == logging.DEBUG
                  and r.name == "cc_harness.agent"
                  and "event_emitter" in r.getMessage()]
    assert debug_msgs, (
        f"emitter 异常应被 _logger.debug 记录;caplog.records="
        f"{[(r.levelname, r.name, r.getMessage()) for r in caplog.records]}"
    )
