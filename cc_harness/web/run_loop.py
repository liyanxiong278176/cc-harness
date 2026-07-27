"""session_run_loop:WS ↔ run_turn 桥 + L2 短路 + L4 / Interrupt 集成。

设计决策(SRP):
  - ws.py 拥有 _consume task(从 event_queue 推 ws)→ run_loop 不开 consumer,
    只处理反向事件 + run_turn bridge。
  - L2:接受 async callable(text) -> ScanResult。让 caller (ws.py / boot.py)
    在路由层 wrap ``cc_harness.l2.scan_user_input(text, l2_cfg=..., client=...,
    model=...)`` 后传入 — run_loop 模块本身不依赖 OpenAI SDK 装配。
  - run_turn:Web session 当前无 MCP 客户端(REPL 用 mcp_factory,web boot 暂未
    装配),用 ``_MCPStub`` 提供空 tool 列表 + error tool result 兜底,plan/
    design mode 仍可正常跑(不调 tool_specs),coding/chat mode 调 tool 时返错
    但不破主 ReAct 循环。
  - _run_turn_for_session 用 ``getattr(rec.state, "messages", None) or []``
    守卫 — rec.state 当前是 ReplState 占位(Task 11+ 才实装),None 时传空 list。
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from cc_harness.l2 import REFUSAL_TEMPLATE, ScanResult
from cc_harness.web.events import (
    deserialize, serialize,
    UserInputEvent, SlashCommand, L4ResponseEvent, InterruptEvent,
    L2RefusedEvent, DoneEvent, ModeEvent, SlashAckEvent, ErrorEvent,
)

log = logging.getLogger(__name__)

# L2 check 签名:caller wrap ``scan_user_input`` 后传入。None = 跳过 L2。
L2Checker = Callable[[str], Awaitable[ScanResult]]


async def session_run_loop(
    rec,
    ws,
    sm,
    llm,
    *,
    l2: Optional[L2Checker] = None,
    l5=None,
) -> None:
    """单 session 的 WS ↔ run_turn 主循环。

    处理 4 类反向事件(前端 → 后端):
      - UserInputEvent: L2 短路 → 触发 run_turn → 等 turn_task 完成
      - SlashCommand:   cmd_to_mode 切 mode → emit ModeEvent + SlashAckEvent
      - L4ResponseEvent: 解决 pending_l4[ask_id] Future(保留字段,实装 L4 提示
                         在 PolicyEngine 与 emitter 桥后续 task 接入)
      - InterruptEvent: cancel 当前 turn_task

    WebSocketDisconnect 向上传播(ws.py 接住做 consumer cleanup)。

    Args:
        rec: SessionRecord
        ws:   FastAPI WebSocket
        sm:   SessionManager
        llm:  LLMClient(任 async chat(messages, tools) -> AsyncIterator 对象)
        l2:   可选 async callable(text) -> ScanResult
        l5:   可选 L5Engine(透传给 EventEmitter)
    """
    pending_l4: dict[str, asyncio.Future] = {}
    turn_task: asyncio.Task | None = None

    async def _send(event) -> None:
        await ws.send_text(serialize(event))

    try:
        while True:
            raw = await ws.receive_text()
            ev = deserialize(f"data: {raw}\n\n")
            if ev is None:
                continue

            if isinstance(ev, UserInputEvent):
                # L2 检查:命中 → 发 L2RefusedEvent + continue(不进 run_turn)
                if l2 is not None:
                    scan = await l2(ev.text)
                    if not scan.allowed:
                        await _send(L2RefusedEvent(template=REFUSAL_TEMPLATE))
                        continue

                # 触发 run_turn(异步 task,允许 InterruptEvent 取消)
                turn_task = asyncio.create_task(
                    _run_turn_for_session(rec, ev.text, llm, sm, l5=l5)
                )
                try:
                    await turn_task
                finally:
                    turn_task = None

            elif isinstance(ev, SlashCommand):
                cmd = ev.command
                new_mode = cmd_to_mode(cmd)
                if new_mode is not None:
                    rec.meta.mode = new_mode
                    await _send(ModeEvent(value=new_mode))
                await _send(SlashAckEvent(command=cmd))

            elif isinstance(ev, L4ResponseEvent):
                # L4 ask resolution:resolve pending Future;若无 pending 则忽略
                # (前端 race 条件兜底)。
                fut = pending_l4.pop(ev.ask_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(ev.decision)

            elif isinstance(ev, InterruptEvent):
                if turn_task is not None and not turn_task.done():
                    turn_task.cancel()
                    try:
                        await turn_task
                    except asyncio.CancelledError:
                        pass
    finally:
        # 兜底清理:WS 断开时若 turn_task 还在跑,取消它。
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except asyncio.CancelledError:
                pass


def cmd_to_mode(cmd: str) -> str | None:
    """Slash command → mode 名(/plan → "plan" 等)。未知命令返 None。"""
    cmd = cmd.lower().lstrip("/")
    if cmd in ("plan", "design", "coding", "chat"):
        return cmd
    return None


class _MCPStub:
    """Web session 无 MCP 客户端时的兜底 stub。

    - list_tools() -> [] 让 plan/design mode 不报 tool_specs 错;
    - call_tool() 返 is_error=True ToolResult,不破 run_turn 主循环
      (coding/chat mode 真要派 tool 时,LLM 拿到错误 tool message 还能继续)。
    """

    def list_tools(self) -> list:
        return []

    async def call_tool(self, name: str, args: dict):
        from cc_harness.mcp_client import ToolResult
        msg = f"[Tool Error] MCP unavailable in web session: {name}"
        return ToolResult(is_error=True, display_text=msg, llm_text=msg)


async def _run_turn_for_session(rec, text: str, llm, sm, *, l5=None) -> None:
    """调 run_turn + 发 DoneEvent + 异常时发 ErrorEvent(fatal=False)。"""
    from cc_harness.web.emitter import EventEmitter
    from cc_harness.agent import run_turn

    emitter = EventEmitter(sm, rec.meta.session_id, l5_engine=l5)
    t0 = time.time()
    try:
        # rec.state 是 ReplState 占位(None / 后续实装)。getattr 守卫:
        # state=None → getattr 返 None → msgs=[]。run_turn 自己处理空 list
        # (_refresh_system_prompt 会 insert system message)。
        msgs = getattr(rec.state, "messages", None) or []
        await run_turn(
            messages=msgs,
            llm=llm,
            mcp=_MCPStub(),
            event_emitter=emitter,
            max_iter=20,
        )
    except Exception as e:
        log.exception("run_turn failed for session %s", rec.meta.session_id)
        await sm.push_event(rec.meta.session_id, ErrorEvent(message=str(e), fatal=False))
    await sm.push_event(rec.meta.session_id, DoneEvent(
        session_id=rec.meta.session_id,
        turn_idx=0,
        duration_ms=int((time.time() - t0) * 1000),
    ))