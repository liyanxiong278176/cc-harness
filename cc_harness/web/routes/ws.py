"""WebSocket chat 流 + PTY 流。"""
from __future__ import annotations
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from cc_harness.web.events import serialize
from cc_harness.web.sessions import SessionManager

router = APIRouter()


@router.websocket("/ws/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    # 版本协商
    version = websocket.headers.get("x-cc-harness-web-version", "0")
    try:
        if int(version) < 1:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    sm: SessionManager = websocket.app.state.session_manager
    rec = await sm.get(session_id)
    if rec is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    # Consumer:从 event_queue 推到 WS(SRP:ws.py 拥有;run_loop 不开 consumer)。
    consumer_task = asyncio.create_task(_consume(websocket, rec))

    # 委托给 session_run_loop(函数体内 import 避免 ws↔run_loop 循环依赖)。
    # l2/l5 kwargs 当前 None — 后续 boot 层在 app.state 暴露后传。
    from cc_harness.web.run_loop import session_run_loop
    try:
        await session_run_loop(rec, websocket, sm, sm.llm)
    except WebSocketDisconnect:
        pass
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass


async def _consume(ws: WebSocket, rec) -> None:
    """从 session.event_queue 推到 WS。"""
    try:
        while True:
            ev = await rec.event_queue.get()
            await ws.send_text(serialize(ev))
    except asyncio.CancelledError:
        pass


# PTY WS(独立连接)— Task 16 实现
@router.websocket("/ws/pty/{pty_id}")
async def ws_pty(websocket: WebSocket, pty_id: str):
    """PTY 双向:前端 stdin ↔ 后端 master_fd,后端 stdout → 前端。"""
    await websocket.accept()
    # TODO:Task 16 实现完整 PTY 桥
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass