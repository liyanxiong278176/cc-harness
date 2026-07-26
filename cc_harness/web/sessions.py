"""SessionManager:in-memory dict + asyncio.Lock。"""
from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Awaitable

from cc_harness.web.events import Event

if TYPE_CHECKING:
    from cc_harness.memory.checkpoint import WebSessionStore


@dataclass
class SessionMeta:
    session_id: str
    cwd: Path
    mode: str
    created_at: float
    last_active_at: float
    status: str = "active"  # 'active' | 'closed' | 'errored'


@dataclass
class SessionRecord:
    meta: SessionMeta
    state: object  # ReplState 占位
    task: asyncio.Task | None = None
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pty_sessions: dict = field(default_factory=dict)


class SessionManager:
    def __init__(
        self,
        llm,
        mcp_factory: Callable[[], Awaitable],
        web_session_store: "WebSessionStore | None" = None,
        max_sessions: int = 8,
    ) -> None:
        self.llm = llm
        self.mcp_factory = mcp_factory
        self.web_session_store = web_session_store
        self.max_sessions = max_sessions
        self._sessions: dict[str, SessionRecord] = {}
        self._llm_lock = asyncio.Lock()
        self._dict_lock = asyncio.Lock()

    async def create(self, cwd: Path, mode: str) -> SessionRecord:
        async with self._dict_lock:
            if len(self._sessions) >= self.max_sessions:
                raise ValueError(f"max_sessions reached ({self.max_sessions})")
            sid = uuid.uuid4().hex
            now = time.time()
            meta = SessionMeta(
                session_id=sid, cwd=cwd, mode=mode,
                created_at=now, last_active_at=now,
            )
            rec = SessionRecord(meta=meta, state=None)
            self._sessions[sid] = rec
        if self.web_session_store is not None:
            await self.web_session_store.upsert(meta)
        return rec

    async def delete(self, session_id: str) -> None:
        async with self._dict_lock:
            rec = self._sessions.pop(session_id, None)
        if rec is None:
            return
        if rec.task and not rec.task.done():
            rec.task.cancel()
            try:
                await asyncio.wait_for(rec.task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if self.web_session_store is not None:
            await self.web_session_store.delete(session_id)

    async def list(self) -> list[SessionMeta]:
        async with self._dict_lock:
            return [r.meta for r in self._sessions.values()]

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._dict_lock:
            return self._sessions.get(session_id)

    async def push_event(self, session_id: str, event: Event) -> None:
        rec = await self.get(session_id)
        if rec is None:
            return
        await rec.event_queue.put(event)

    async def restore_from_checkpoint(self) -> None:
        """Task 9 实现。stub 留空。"""
        if self.web_session_store is None:
            return
        # Task 9 will fill
        pass
