"""Detached local supervisor for queued durable runs."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass

from .followups import FollowUpService
from .run_model import RunStatus
from .lease import LeaseManager
from .run_store import RunStore
from .worker import RunWorker


WorkerFactory = Callable[[str], RunWorker]


@dataclass(frozen=True)
class SupervisorStats:
    active_runs: tuple[str, ...]
    queued_runs: int


class LocalSupervisor:
    def __init__(
        self,
        store: RunStore,
        worker_factory: WorkerFactory,
        *,
        max_workers: int = 3,
        poll_interval: float = 0.25,
    ) -> None:
        self.store = store
        self.worker_factory = worker_factory
        self.max_workers = max(1, max_workers)
        self.poll_interval = max(0.01, poll_interval)
        self._active: dict[str, tuple[asyncio.Task, RunWorker]] = {}
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self._lease_manager = LeaseManager(store)
        self._followups = FollowUpService(store)

    async def start(self) -> None:
        if self._loop_task is None:
            self._stopping = False
            self._loop_task = asyncio.create_task(self._run_loop(), name="cc-harness-supervisor")

    async def tick(self) -> SupervisorStats:
        finished = [run_id for run_id, (task, _worker) in self._active.items() if task.done()]
        for run_id in finished:
            task, _worker = self._active.pop(run_id)
            # Retrieve failures so a fenced/crashed worker does not become an
            # unobserved asyncio warning.  The durable lease scan below is the
            # recovery authority for a still-running stream.
            with contextlib.suppress(asyncio.CancelledError):
                task.exception()
        # A completed/cancelled predecessor releases ordinary follow-ups.  The
        # queue remains event-sourced on the predecessor; materialization is
        # deliberately done here so closing a client never cancels queued work.
        for record in await self.store.list_runs():
            try:
                await self._followups.release_ready(record.run_id)
            except Exception:  # noqa: BLE001 - a malformed queue item cannot stop siblings
                continue
        # A process crash leaves a RUNNING stream and its lease in SQLite.
        # Reclaim only after the lease expiry; the resulting event moves the
        # run back to the queue and fences the old worker epoch.
        running = await self.store.list_runs(
            {RunStatus.RUNNING.value, RunStatus.CANCEL_REQUESTED.value}
        )
        for record in running:
            if record.run_id in self._active:
                continue
            try:
                await self._lease_manager.reclaim_expired(record.run_id)
            except Exception:  # noqa: BLE001 - a single stale run cannot stop siblings
                continue
        capacity = self.max_workers - len(self._active)
        if capacity > 0:
            records = await self.store.list_runs({RunStatus.QUEUED.value})
            for record in records[:capacity]:
                if record.run_id in self._active:
                    continue
                worker = self.worker_factory(record.run_id)
                try:
                    lease = await worker.claim(record.run_id)
                except Exception:  # noqa: BLE001 - one run cannot stop siblings
                    continue
                task = asyncio.create_task(worker.execute(lease), name=f"cc-harness-worker-{record.run_id}")
                self._active[record.run_id] = (task, worker)
        queued = await self.store.list_runs({RunStatus.QUEUED.value})
        return SupervisorStats(tuple(sorted(self._active)), len(queued))

    async def stop(self, drain: bool = True) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        tasks = [task for task, _worker in self._active.values()]
        if not drain:
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    async def _run_loop(self) -> None:
        while not self._stopping:
            await self.tick()
            await asyncio.sleep(self.poll_interval)

__all__ = ["LocalSupervisor", "SupervisorStats"]
