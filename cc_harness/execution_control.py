"""Event-sourced task controls used by clients, workers and the supervisor."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import replace
from typing import Any, Mapping

from .run_events import EventActor, RunEvent
from .run_model import ActionStatus, RunStatus
from .run_store import RunStore, SequenceConflict
from .task_execution import TERMINAL_TASKS, budget_values


class BudgetExhausted(RuntimeError):
    pass


class ExecutionControl:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        # One reservation coordinator per authoritative writer. No worker-local balances.
        if not hasattr(store, "_execution_control_lock"):
            store._execution_control_lock = asyncio.Lock()
        self.lock = store._execution_control_lock
        if not hasattr(store, "_task_receivers"):
            store._task_receivers = {}
        self.receivers = store._task_receivers

    def register_receiver(self, name: str, worker_factory) -> None:
        if not name.strip() or name == "main" or not callable(worker_factory):
            raise ValueError("receiver needs a distinct name and a worker factory")
        if name in self.receivers:
            raise ValueError("receiver is already registered")
        self.receivers[name] = worker_factory

    async def append(self, run_id: str, event_type: str, payload: Mapping[str, Any]):
        async with self.store.event_append_lock:
            return await self._append_locked(run_id, event_type, payload)

    async def _append_locked(self, run_id: str, event_type: str, payload: Mapping[str, Any]):
        for attempt in range(12):
            lease = getattr(self, "worker_lease", None)
            if lease is not None:
                from .run_store import LeaseFenceError
                current = await self.store.current_lease(lease.run_id)
                if current is None or current.epoch != lease.epoch or current.worker_id != lease.worker_id:
                    raise LeaseFenceError("task control writer has lost its worker lease")
            projection = await self.store.load_projection(run_id)
            event = RunEvent.create(
                run_id=run_id, sequence=projection.sequence + 1, event_type=event_type,
                actor=EventActor("client", "execution-control"),
                runtime_contract_digest=str(projection.runtime_contract_digest), payload=dict(payload),
            )
            try:
                await self.store.append(event, expected_sequence=projection.sequence)
                return event
            except SequenceConflict:
                if attempt == 11:
                    raise
        raise RuntimeError("unreachable")

    async def enable(self, run_id: str) -> None:
        projection = await self.store.load_projection(run_id)
        if not projection.task.protocol_version:
            await self.append(run_id, "TaskControlEnabled", {})

    async def configure_budget(self, run_id: str, limits: Mapping[str, Any]) -> None:
        await self.append(run_id, "TaskBudgetConfigured", {"limits": budget_values(limits)})

    async def lineage(self, run_id: str) -> tuple[str, ...]:
        parents = {r.run_id: (None if r.predecessor_run_id else r.parent_run_id)
                   for r in await self.store.list_runs()}
        result = []
        while run_id:
            if run_id in result:
                raise ValueError("cyclic task ancestry")
            result.append(run_id)
            run_id = parents.get(run_id)
        return tuple(reversed(result))

    async def reserve(self, run_id: str, amounts: Mapping[str, Any], *, key: str | None = None) -> str:
        amounts = budget_values(amounts)
        key = key or str(uuid.uuid4())
        async with self.lock:
            lineage = await self.lineage(run_id)
            projections = [await self.store.load_projection(item) for item in lineage]
            for projection in projections:
                if projection.task.pause_requested or projection.status in {
                    RunStatus.PAUSED, RunStatus.CANCELLED, RunStatus.CANCEL_REQUESTED,
                    RunStatus.COMPLETED, RunStatus.FAILED_TERMINAL,
                }:
                    raise BudgetExhausted("task or ancestor is stopped")
                if key in projection.task.reservations:
                    raise ValueError("reservation key already in use")
                missing = projection.task.unavailable(amounts)
                if missing:
                    await self.pause(run_id, "budget_exhausted: " + ", ".join(missing))
                    raise BudgetExhausted(", ".join(missing))
            committed = []
            try:
                for item in lineage:
                    await self.append(item, "TaskBudgetReserved", {
                        "reservation_id": key, "owner_run_id": run_id, "amounts": amounts,
                    })
                    committed.append(item)
            except BaseException:
                # A process crash may leave conservative reservations. They remain visible
                # and require reconciliation; never silently reset them on restart.
                for item in committed:
                    await asyncio.shield(self.append(item, "TaskBudgetSettled", {
                        "reservation_id": key, "actual": {},
                    }))
                raise
        return key

    async def settle(self, run_id: str, key: str, actual: Mapping[str, Any], *, estimated=()) -> None:
        actual = budget_values(actual)
        async with self.lock:
            for item in await self.lineage(run_id):
                projection = await self.store.load_projection(item)
                if key not in projection.task.reservations:
                    continue  # settlement retries are idempotent
                await self.append(item, "TaskBudgetSettled", {
                    "reservation_id": key, "actual": actual, "estimated_dimensions": list(estimated),
                })

    async def remaining_time(self, run_id: str) -> float | None:
        values = []
        for item in await self.lineage(run_id):
            task = (await self.store.load_projection(item)).task
            if "active_seconds" in task.limits:
                values.append(max(0, task.limits["active_seconds"] - task.used.get("active_seconds", 0)))
        return min(values) if values else None

    async def recover_reservations(self, run_id: str) -> None:
        """Conservatively account abandoned requests; never infer their side effects."""
        if await self.store.current_lease(run_id) is not None:
            raise ValueError("reservation recovery requires no active worker lease")
        projection = await self.store.load_projection(run_id)
        for key, reservation in tuple(projection.task.reservations.items()):
            if reservation["owner_run_id"] != run_id:
                continue
            actual = dict(reservation["amounts"])
            if "active_seconds" in projection.task.limits:
                actual["active_seconds"] = max(
                    actual.get("active_seconds", 0),
                    projection.task.limits["active_seconds"] - projection.task.used.get("active_seconds", 0),
                )
            await self.settle(run_id, key, actual, estimated=tuple(actual))

    async def pause(self, run_id: str, reason: str, *, cascade: bool = True) -> None:
        projection = await self.store.load_projection(run_id)
        if projection.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED_TERMINAL}:
            return
        if projection.status is not RunStatus.PAUSED:
            if projection.status is RunStatus.RUNNING:
                if not projection.task.pause_requested:
                    await self.append(run_id, "TaskPauseRequested", {"reason": reason})
            else:
                await self.append(run_id, "RunPaused", {"reason": reason})
        if cascade:
            for child in projection.children:
                await self.pause(child.child_run_id, reason, cascade=True)

    async def pause_boundary(self, run_id: str) -> bool:
        projection = await self.store.load_projection(run_id)
        if projection.status is RunStatus.PAUSED:
            return True
        if projection.task.pause_requested:
            for action in projection.actions:
                if action.status is ActionStatus.STARTED and action.effect_class != "read_only":
                    await self.append(run_id, "ActionOutcomeUnknown", {
                        "action_id": action.action_id, "attempt": action.attempt,
                        "reason": "pause reached a recovery boundary without a durable action result",
                    })
            await self.append(run_id, "RunPaused", {"reason": projection.task.reason})
            await self.store.checkpoint(run_id)
            return True
        return False

    async def guidance(self, run_id: str, text: str, *, affected_children=None) -> str:
        if not text.strip():
            raise ValueError("guidance cannot be empty")
        projection = await self.store.load_projection(run_id)
        if projection.task.lifecycle.value in TERMINAL_TASKS and projection.status not in {
            RunStatus.FAILED_RECOVERABLE,
        }:
            raise ValueError("terminal task cannot receive guidance")
        known = {child.child_run_id for child in projection.children}
        # Without an explicit dependency/path proof, conservatively update all active
        # children. Callers can provide a precise affected subset; never guess none.
        selected = known if affected_children is None else set(affected_children)
        if not selected.issubset(known):
            raise ValueError("guidance references an unrelated child")
        key = str(uuid.uuid4())
        await self.append(run_id, "TaskGuidanceReceived", {
            "guidance_id": key, "text": text, "affected_children": sorted(selected),
        })
        for approval in projection.approvals:
            if approval.status.value in {"requested", "granted"}:
                await self.append(run_id, "TaskApprovalSuperseded", {
                    "approval_id": approval.approval_id, "reason": "task_requirements_changed",
                })
        if projection.status is RunStatus.WAITING_EVENT and projection.task.wait.get("kind") == "waiting_input":
            await self.wake(run_id, str(projection.task.wait["wait_id"]))
        for child_id in sorted(selected):
            child = await self.store.load_projection(child_id)
            if child.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED_TERMINAL}:
                continue
            await self.guidance(child_id, text)
        return key

    async def apply_guidance(self, run_id: str) -> bool:
        projection = await self.store.load_projection(run_id)
        pending = [x for x in projection.task.guidance if not x["applied"]]
        if not pending:
            return False
        if projection.goal is None:
            raise ValueError("task has no authoritative goal")
        goal = replace(
            projection.goal,
            objective=projection.goal.objective + "\n\nUser updates:\n" + "\n".join(
                f"- {x['text']}" for x in pending
            ),
            contract_version=projection.goal.contract_version + 1,
        )
        await self.append(run_id, "GoalContractRevised", {
            "goal": goal.to_dict(),
            "guidance_ids": [x["guidance_id"] for x in pending],
        })
        return True

    async def wait(self, run_id: str, *, kind: str, reason: str, wait_id: str | None = None,
                   timeout_at: float | None = None) -> str:
        wait_id = wait_id or str(uuid.uuid4())
        await self.append(run_id, "TaskWaitStarted", {
            "wait_id": wait_id, "kind": kind, "reason": reason, "timeout_at": timeout_at,
        })
        for record in await self.store.list_runs():
            if record.run_id == wait_id and record.parent_run_id == run_id and record.status in {
                "completed", "cancelled", "failed_terminal", "failed_recoverable",
            }:
                await self.wake(run_id, wait_id)
                break
        return wait_id

    async def wake(self, run_id: str, wait_id: str) -> bool:
        projection = await self.store.load_projection(run_id)
        if not projection.task.wait:
            return False
        if projection.task.wait.get("wait_id") != wait_id or projection.status is RunStatus.PAUSED:
            return False
        await self.append(run_id, "TaskWaitResolved", {"wait_id": wait_id})
        return True

    async def attach_terminal(self, run_id: str, pid: int | None = None) -> None:
        pid = pid or os.getpid()
        from .terminal_owner import process_identity
        identity = process_identity(pid)
        if identity is None:
            raise ValueError("terminal process is not observable")
        await self.append(run_id, "TaskTerminalAttached", {"pid": pid, "identity": identity})

    async def inspect_waits_and_terminals(self) -> None:
        from .terminal_owner import process_identity
        for record in await self.store.list_runs():
            if record.status in {"completed", "cancelled", "failed_terminal", "paused"}:
                continue
            try:
                projection = await self.store.load_projection(record.run_id)
            except Exception:
                continue  # isolate damaged history; the scheduler records its diagnostic
            owner = projection.task.owner_terminal
            if owner and process_identity(int(owner["pid"])) != owner["identity"]:
                await self.pause(record.run_id, "terminal_closed")
            deadline = projection.task.wait.get("timeout_at")
            if deadline is not None and time.time() >= float(deadline):
                await self.pause(record.run_id, "wait_timeout")

    async def recover(self) -> None:
        """Only the elected supervisor calls this before dispatch after startup."""
        for record in await self.store.list_runs():
            if record.status not in {"running", "cancel_requested"}:
                continue
            lease = await self.store.current_lease(record.run_id)
            if lease is not None and not lease.is_expired():
                continue  # another live worker must first be fenced
            projection = await self.store.load_projection(record.run_id)
            if projection.task.protocol_version:
                await self.pause(record.run_id, "recovery_requires_continuation")
                await self.pause_boundary(record.run_id)

    async def migrate_legacy(self, run_id: str) -> None:
        projection = await self.store.load_projection(run_id)
        if projection.task.protocol_version:
            return
        lease = await self.store.current_lease(run_id)
        if lease is not None:
            raise ValueError("migration requires a quiescent task without a worker lease")
        from .runtime_contract import RuntimeContractService
        contract = projection.runtime_contract
        if contract is None:
            raise ValueError("legacy task has no verifiable runtime contract")
        await RuntimeContractService().migrate(
            self.store, run_id, replace(contract, runtime_version=contract.runtime_version + "+task-execution-v1"),
            reason="task execution kernel migration; preserve historical events and evidence",
        )
        await self.enable(run_id)
        if projection.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED_TERMINAL}:
            await self.pause(run_id, "legacy_task_migrated_requires_continuation")
            await self.pause_boundary(run_id)

    async def reconcile_reservation(self, run_id: str, key: str, actual: Mapping[str, Any],
                                    *, evidence: str) -> None:
        if not evidence or not self.store.artifacts.exists(evidence):
            raise ValueError("reconciliation requires an existing evidence artifact")
        if await self.store.current_lease(run_id) is not None:
            raise ValueError("cannot reconcile a reservation owned by a live worker")
        await self.settle(run_id, key, actual)

    async def handoff(self, run_id: str, *, to_owner: str, continuation_artifact: str) -> None:
        if (to_owner != "main" and to_owner not in self.receivers) or not self.store.artifacts.exists(continuation_artifact):
            raise ValueError("handoff requires a receiver and verifiable continuation artifact")
        projection = await self.store.load_projection(run_id)
        if projection.status is not RunStatus.PAUSED or await self.store.current_lease(run_id) is not None:
            raise ValueError("handoff requires a paused task without a live worker")
        await self.append(run_id, "TaskHandoffRecorded", {
            "from_owner": projection.task.owner, "to_owner": to_owner,
            "continuation_artifact": continuation_artifact,
        })
