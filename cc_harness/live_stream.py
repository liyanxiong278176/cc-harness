"""Ephemeral in-process stream fan-out for the WebUI.

Durable Runtime events are the recovery and audit authority.  This module is
deliberately *not* a second event store: it only keeps a small, bounded ring of
in-flight presentation messages so an attached WebUI can render provider
chunks without waiting for ``AssistantMessageCommitted``.  A reconnect must
always reconcile against the durable timeline first.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from itertools import count
from typing import Any


class LiveStreamHub:
    """Publish bounded, best-effort stream messages to local subscribers.

    Publishing never waits for a slow browser and never raises into the
    worker.  If a subscriber queue is full, the oldest ephemeral delta is
    dropped and a ``stream_gap`` marker is queued; the SSE consumer then
    relies on its normal Durable timeline reconciliation.
    """

    def __init__(self, *, history_limit: int = 256, queue_limit: int = 256) -> None:
        self.history_limit = max(1, int(history_limit))
        self.queue_limit = max(1, int(queue_limit))
        self._lock = asyncio.Lock()
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._subscriber_ids = count(1)
        self._live_ids = count(1)

    async def publish(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """Publish one ephemeral message and return its normalized envelope."""

        run_id = str(message.get("run_id") or "").strip()
        if not run_id:
            return None
        envelope = {str(key): value for key, value in message.items()}
        envelope["run_id"] = run_id
        envelope.setdefault("type", "stream_delta")
        envelope.setdefault("ts", time.time())
        envelope["live_id"] = next(self._live_ids)

        async with self._lock:
            self._history[run_id].append(dict(envelope))
            for queue in tuple(self._subscribers.values()):
                self._offer(queue, envelope)
        return envelope

    def _offer(self, queue: asyncio.Queue[dict[str, Any]], envelope: Mapping[str, Any]) -> None:
        try:
            queue.put_nowait(dict(envelope))
            return
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        gap = {
            "type": "stream_gap",
            "run_id": envelope["run_id"],
            "live_id": envelope["live_id"],
            "reason": "subscriber_queue_full",
            "ts": envelope.get("ts", time.time()),
        }
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(gap)

    async def recent(self, run_ids: set[str] | frozenset[str]) -> list[dict[str, Any]]:
        """Return a stable, bounded history snapshot for selected runs."""

        wanted = {str(run_id) for run_id in run_ids}
        async with self._lock:
            values = [
                dict(item)
                for run_id in wanted
                for item in self._history.get(run_id, ())
            ]
        values.sort(key=lambda item: int(item.get("live_id", 0)))
        return values

    async def subscribe(self) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        """Register a subscriber; callers must later call :meth:`unsubscribe`."""

        token = next(self._subscriber_ids)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.queue_limit)
        async with self._lock:
            self._subscribers[token] = queue
        return token, queue

    async def unsubscribe(self, token: int) -> None:
        async with self._lock:
            self._subscribers.pop(token, None)

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Yield a queue and always remove it when the HTTP stream closes."""

        token, queue = await self.subscribe()
        try:
            yield queue
        finally:
            await self.unsubscribe(token)

    async def close(self) -> None:
        """Drop history and subscribers during WebUI shutdown."""

        async with self._lock:
            self._history.clear()
            self._subscribers.clear()

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            return {
                "subscribers": len(self._subscribers),
                "runs": len(self._history),
                "messages": sum(len(items) for items in self._history.values()),
            }


__all__ = ["LiveStreamHub"]
