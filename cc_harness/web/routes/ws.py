"""WebSocket chat 流 + PTY 流。"""
from __future__ import annotations
import asyncio
import base64
import json
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

    # L2 / L5 在 create_app 注入到 app.state(测试 / boot 路径)。getattr
    # 兜底:旧 create_app 调用未传 l2/l5 时返 None,session_run_loop 内部
    # 守卫跳过对应层(L2 不扫 + L5 不脱敏)。
    l2_checker = getattr(websocket.app.state, "l2", None)
    l5_engine = getattr(websocket.app.state, "l5", None)

    # 委托给 session_run_loop(函数体内 import 避免 ws↔run_loop 循环依赖)。
    from cc_harness.web.run_loop import session_run_loop
    try:
        await session_run_loop(rec, websocket, sm, sm.llm, l2=l2_checker, l5=l5_engine)
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


@router.websocket("/ws/pty/{pty_id}")
async def ws_pty(websocket: WebSocket, pty_id: str):
    await websocket.accept()
    pm = getattr(websocket.app.state, "pty_manager", None)
    if pm is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    rec = pm.get(pty_id)
    if rec is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # stdin 协程:从 WS 写到 master_fd
    async def _stdin():
        try:
            while True:
                raw = await websocket.receive_text()
                ev = json.loads(raw)
                if ev.get("type") == "stdin":
                    data = base64.b64decode(ev["data"])
                    await pm.write_stdin(pty_id, data)
                elif ev.get("type") == "exit":
                    break
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    # stdout 协程:从 stdout_queue 推到 WS
    async def _stdout():
        try:
            while True:
                chunk = await rec.stdout_queue.get()
                await websocket.send_json({
                    "type": "stdout",
                    "data": base64.b64encode(chunk).decode("ascii"),
                })
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    stdin_task = asyncio.create_task(_stdin())
    stdout_task = asyncio.create_task(_stdout())
    try:
        await asyncio.gather(stdin_task, stdout_task)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        stdin_task.cancel()
        stdout_task.cancel()
        await asyncio.gather(stdin_task, stdout_task, return_exceptions=True)
        try:
            await websocket.close()
        except (WebSocketDisconnect, RuntimeError):
            pass
