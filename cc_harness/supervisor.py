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
            records = await self._select_ready_records(capacity)
            for record in records:
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

    async def _select_ready_records(self, capacity: int):
        """Select only dependency-ready work from the run PlanGraphs.

        Root runs are serialized per project. Child runs can share capacity only
        when their parent graph declares the nodes ready and their owned paths
        are disjoint. Follow-ups use the predecessor gate and never inherit a
        parent's full transcript as a scheduling shortcut.
        """

        records = await self.store.list_runs({RunStatus.QUEUED.value})
        if not records:
            return ()
        all_records = await self.store.list_runs()
        projections = {
            record.run_id: await self.store.load_projection(record.run_id)
            for record in all_records
            if record.run_id not in self._active
        }
        active_records = [
            record
            for record in all_records
            if record.status
            in {
                RunStatus.RUNNING.value,
                RunStatus.CANCEL_REQUESTED.value,
                RunStatus.AWAITING_APPROVAL.value,
            }
        ]
        # The store is project-scoped: an active child/follow-up also occupies
        # the project root gate, even if its parent root has yielded.
        active_root = bool(active_records)
        selected = []
        selected_roots = 0
        selected_child = False
        selected_child_paths: list[str] = []
        selected_child_worktrees: list[str] = []
        active_child_paths: list[str] = []
        active_child_worktrees: list[str] = []
        active_unscoped_child = False
        active_unisolated_child = False
        for active in active_records:
            if active.parent_run_id is None:
                continue
            paths = await self._record_owned_paths(active, projections)
            worktree = await self._record_worktree(active, projections)
            active_child_paths.extend(paths)
            if worktree:
                active_child_worktrees.append(worktree)
            else:
                active_unisolated_child = True
            active_unscoped_child = active_unscoped_child or (not paths and not worktree)
        selected_unscoped_child = False
        selected_unisolated_child = False
        for record in records:
            if len(selected) >= capacity:
                break
            if record.run_id in self._active:
                continue
            projection = projections.get(record.run_id)
            if projection is not None and projection.discovery_status == "awaiting":
                continue
            if record.parent_run_id is None:
                if active_root or selected_roots or selected_child:
                    continue
                selected.append(record)
                selected_roots += 1
                continue
            if selected_roots:
                continue
            if not await self._record_is_ready(
                record,
                all_records=all_records,
                projections=projections,
                selected_child_paths=selected_child_paths,
            ):
                continue
            node_paths = await self._record_owned_paths(record, projections)
            node_worktree = await self._record_worktree(record, projections)
            if active_unscoped_child or selected_unscoped_child:
                continue
            if active_unisolated_child or selected_unisolated_child:
                continue
            if node_worktree and node_worktree in (*active_child_worktrees, *selected_child_worktrees):
                continue
            if any(
                _paths_overlap(left, right)
                for left in node_paths
                for right in (*selected_child_paths, *active_child_paths)
            ):
                continue
            # An unscoped child has no proof that it owns an isolated
            # workspace. It may run alone, but it cannot join a parallel
            # cohort with another active/selected child.
            if not node_paths and (selected_child_paths or active_child_paths):
                continue
            if not node_worktree and (
                selected_child_paths
                or active_child_paths
                or selected_child_worktrees
                or active_child_worktrees
            ):
                continue
            if not node_paths and any(
                item.parent_run_id == record.parent_run_id for item in all_records
                if item.run_id != record.run_id and item.status in {
                    RunStatus.RUNNING.value,
                    RunStatus.AWAITING_APPROVAL.value,
                    RunStatus.CANCEL_REQUESTED.value,
                }
            ):
                continue
            selected.append(record)
            selected_child = True
            selected_child_paths.extend(node_paths)
            if node_worktree:
                selected_child_worktrees.append(node_worktree)
            else:
                selected_unisolated_child = True
            selected_unscoped_child = selected_unscoped_child or (not node_paths and not node_worktree)
        return tuple(selected)

    async def _record_is_ready(self, record, *, all_records, projections, selected_child_paths) -> bool:
        if record.predecessor_run_id:
            predecessor = projections.get(record.predecessor_run_id)
            if predecessor is None:
                predecessor = await self.store.load_projection(record.predecessor_run_id)
            if predecessor.status not in {
                RunStatus.COMPLETED,
                RunStatus.CANCELLED,
                RunStatus.FAILED_TERMINAL,
            }:
                return False
        parent = projections.get(record.parent_run_id)
        if parent is None:
            parent = await self.store.load_projection(record.parent_run_id)
        if parent.discovery_status == "awaiting":
            return False
        child = next(
            (item for item in parent.children if item.child_run_id == record.run_id),
            None,
        )
        if child is None:
            # A follow-up has a parent/predecessor but no child PlanNode.
            return bool(record.predecessor_run_id)
        node = next((item for item in parent.plan.nodes if item.node_id == child.node_id), None)
        if node is None:
            return False
        completed = await self._completed_plan_nodes(parent.run_id, parent)
        if any(dependency not in completed for dependency in node.depends_on):
            return False
        active_children = [
            item
            for item in all_records
            if item.parent_run_id == record.parent_run_id
            and item.status
            in {RunStatus.RUNNING.value, RunStatus.CANCEL_REQUESTED.value, RunStatus.AWAITING_APPROVAL.value}
        ]
        if len(active_children) >= parent.plan.max_concurrent_children:
            return False
        return True

    async def _completed_plan_nodes(self, run_id: str, projection) -> set[str]:
        completed = {
            todo.todo_id
            for todo in projection.todos
            if todo.status in {"done", "completed"}
        }
        page = await self.store.read(run_id, limit=100_000)
        completed.update(
            str(event.payload["node_id"])
            for event in page.events
            if event.event_type == "PlanNodeCompleted"
        )
        return completed

    async def _record_owned_paths(self, record, projections) -> tuple[str, ...]:
        if not record.parent_run_id:
            return ()
        parent = projections.get(record.parent_run_id)
        if parent is None:
            parent = await self.store.load_projection(record.parent_run_id)
        child = next((item for item in parent.children if item.child_run_id == record.run_id), None)
        if child is None:
            return ()
        node = next((item for item in parent.plan.nodes if item.node_id == child.node_id), None)
        return tuple(node.owned_paths) if node is not None else ()

    async def _record_worktree(self, record, projections) -> str | None:
        if not record.parent_run_id:
            return None
        parent = projections.get(record.parent_run_id)
        if parent is None:
            parent = await self.store.load_projection(record.parent_run_id)
        child = next((item for item in parent.children if item.child_run_id == record.run_id), None)
        if child is None:
            return None
        node = next((item for item in parent.plan.nodes if item.node_id == child.node_id), None)
        return node.worktree_id if node is not None else None

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


def _paths_overlap(left: str, right: str) -> bool:
    a = left.replace("\\", "/").strip().rstrip("/") or "."
    b = right.replace("\\", "/").strip().rstrip("/") or "."
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")
