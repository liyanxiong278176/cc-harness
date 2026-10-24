"""Durable ordinary-message queue and predecessor gate handling."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, replace

from .artifacts import ArtifactStore
from .run_events import EventActor, RunEvent
from .run_model import GoalContract, PlanGraph, PlanNode, Run, RunStatus, RuntimeContract, predecessor_gate
from .run_projection import QueueProjection
from .run_store import RunStore, RunStoreError


@dataclass(frozen=True)
class FollowUpReceipt:
    predecessor_run_id: str
    follow_up_run_id: str
    sequence: int
    gate: str
    started: bool


# ``enqueue`` (coordinator/webui) and ``release_ready`` (supervisor) run on
# different FollowUpService instances but may start the same queued item at the
# same time.  Both would then try to append ``RunCreated`` for the same child
# run: one wins, the other raises "RunCreated can only be the first event".
# A process-wide lock keyed by store serializes the start sequence; durable
# cross-process conflicts are still caught by the store's sequence check.
_START_LOCKS: dict[int, asyncio.Lock] = {}


def _start_lock(store) -> asyncio.Lock:
    lock = _START_LOCKS.get(id(store))
    if lock is None:
        lock = asyncio.Lock()
        _START_LOCKS[id(store)] = lock
    return lock


class FollowUpService:
    """Queue normal messages and materialize each as its own Run."""

    def __init__(self, store: RunStore, *, artifacts: ArtifactStore | None = None) -> None:
        self.store = store
        self.artifacts = artifacts or store.artifacts
        self.actor = EventActor("client", "local-client")

    async def enqueue(self, predecessor_run_id: str, message: str, *, continuation: bool = False) -> FollowUpReceipt:
        if not message.strip():
            raise ValueError("follow-up message cannot be empty")
        artifact = self.artifacts.put_text(message, media_type="text/plain; charset=utf-8")
        predecessor = await self.store.load_projection(predecessor_run_id)
        gate = predecessor_gate(predecessor.status).value
        follow_up_run_id = str(uuid.uuid4())
        event = self._event(
            predecessor,
            "FollowUpQueued",
            {
                "follow_up_run_id": follow_up_run_id,
                "predecessor_run_id": predecessor_run_id,
                "message_artifact": artifact.digest,
                "gate": gate,
                "continuation": continuation,
            },
        )
        await self.store.append(event, expected_sequence=predecessor.sequence)
        started = gate in {"ready", "incomplete"} and not await self._has_active_predecessor(predecessor.queue)
        if started:
            await self._start(predecessor_run_id, follow_up_run_id, artifact.digest, incomplete=gate == "incomplete")
        return FollowUpReceipt(predecessor_run_id, follow_up_run_id, event.sequence, gate, started)

    async def release_ready(self, predecessor_run_id: str) -> tuple[FollowUpReceipt, ...]:
        predecessor = await self.store.load_projection(predecessor_run_id)
        ready: list[FollowUpReceipt] = []
        for item in predecessor.queue:
            if item.status == "started":
                child = await self.store.load_projection(item.follow_up_run_id)
                if child.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.STALLED, RunStatus.FAILED_RECOVERABLE, RunStatus.FAILED_TERMINAL, RunStatus.BLOCKED}:
                    break
            if item.status not in {"queued", "starting"}:
                continue
            gate = predecessor_gate(predecessor.status, bypassed=item.gate == "bypassed").value
            if gate not in {"ready", "incomplete", "bypassed"}:
                continue
            await self._start(
                predecessor_run_id,
                item.follow_up_run_id,
                item.message_artifact,
                incomplete=gate in {"incomplete", "bypassed"},
            )
            updated = await self.store.load_projection(predecessor_run_id)
            ready.append(
                FollowUpReceipt(
                    predecessor_run_id,
                    item.follow_up_run_id,
                    updated.sequence,
                    gate,
                    True,
                )
            )
            predecessor = updated
            break  # Release one message at a time to preserve FIFO execution.
        return tuple(ready)

    async def _has_active_predecessor(self, queue) -> bool:
        for item in queue:
            if item.status in {"queued", "starting"}:
                return True
            if item.status == "started":
                child = await self.store.load_projection(item.follow_up_run_id)
                if child.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.STALLED, RunStatus.FAILED_RECOVERABLE, RunStatus.FAILED_TERMINAL, RunStatus.BLOCKED}:
                    return True
        return False

    async def bypass(self, predecessor_run_id: str, follow_up_run_id: str, reason: str) -> FollowUpReceipt:
        predecessor = await self.store.load_projection(predecessor_run_id)
        item = self._find_item(predecessor.queue, follow_up_run_id)
        if item.status != "queued":
            raise ValueError(f"follow-up is not queued: {follow_up_run_id}")
        event = self._event(
            predecessor,
            "PredecessorBypassed",
            {
                "predecessor_run_id": predecessor_run_id,
                "follow_up_run_id": follow_up_run_id,
                "reason": reason,
            },
        )
        await self.store.append(event, expected_sequence=predecessor.sequence)
        await self._start(predecessor_run_id, follow_up_run_id, item.message_artifact, incomplete=True)
        updated = await self.store.load_projection(predecessor_run_id)
        return FollowUpReceipt(predecessor_run_id, follow_up_run_id, updated.sequence, "bypassed", True)

    async def revise(self, predecessor_run_id: str, follow_up_run_id: str, message: str | None) -> int:
        """Revise only unclaimed work, using the event sequence as a CAS fence."""
        predecessor = await self.store.load_projection(predecessor_run_id)
        item = self._find_item(predecessor.queue, follow_up_run_id)
        if item.status != "queued" or await self.store.run_exists(follow_up_run_id):
            raise ValueError("消息已开始处理，不能修改或撤回")
        payload = {"follow_up_run_id": follow_up_run_id}
        kind = "FollowUpCancelled"
        if message is not None:
            if not message.strip():
                raise ValueError("消息不能为空")
            payload["message_artifact"] = self.artifacts.put_text(message.strip()).digest
            kind = "FollowUpUpdated"
        event = self._event(predecessor, kind, payload)
        await self.store.append(event, expected_sequence=predecessor.sequence)
        return event.sequence

    async def _start(
        self,
        predecessor_run_id: str,
        follow_up_run_id: str,
        message_artifact: str,
        *,
        incomplete: bool,
    ) -> None:
        # Serialized per store: two services may start the same queued item.
        async with _start_lock(self.store):
            await self._start_unlocked(
                predecessor_run_id,
                follow_up_run_id,
                message_artifact,
                incomplete=incomplete,
            )

    async def _start_unlocked(
        self,
        predecessor_run_id: str,
        follow_up_run_id: str,
        message_artifact: str,
        *,
        incomplete: bool,
    ) -> None:
        predecessor = await self.store.load_projection(predecessor_run_id)
        item = self._find_item(predecessor.queue, follow_up_run_id)
        if item.status == "started":
            return
        if item.status == "cancelled":
            return
        if item.status == "queued":
            claim = self._event(predecessor, "FollowUpClaimed", {"follow_up_run_id": follow_up_run_id})
            await self.store.append(claim, expected_sequence=predecessor.sequence)
            predecessor = await self.store.load_projection(predecessor_run_id)
            item = self._find_item(predecessor.queue, follow_up_run_id)
        # Always use the latest durable revision, including after a crash.
        message_artifact = item.message_artifact
        contract = predecessor.runtime_contract
        if contract is None:
            raise ValueError("predecessor has no pinned runtime contract")
        message = self.artifacts.read_text(message_artifact)
        constraints = (f"predecessor_run_id={predecessor_run_id}",)
        if incomplete:
            constraints += (
                "predecessor_outcome_may_be_incomplete",
                "前序成果可能不完整；必须显式验证前序事实。",
            )
        goal = GoalContract(
            objective=message,
            acceptance_criteria=("follow-up request addressed",),
            constraints=constraints,
            interaction_mode=predecessor.goal.interaction_mode if predecessor.goal else "coding",
        )
        if item.continuation and predecessor.goal is not None:
            goal = replace(predecessor.goal,
                constraints=(*predecessor.goal.constraints, *constraints),
                contract_version=predecessor.goal.contract_version + 1)
        handoff = self.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": "cc-harness.predecessor-handoff.v1",
                    "predecessor_run_id": predecessor_run_id,
                    "follow_up_run_id": follow_up_run_id,
                    "message": message,
                    "predecessor_status": predecessor.status.value,
                    "authorized_recall": {
                        "run_ids": [predecessor_run_id],
                        "include_transcript": False,
                        "include_artifacts": True,
                    },
                    "constraints": list(constraints),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            media_type="application/json; purpose=predecessor-handoff",
        )
        if not await self.store.run_exists(follow_up_run_id):
            try:
                await self.store.create_run(
                    Run(
                        run_id=follow_up_run_id,
                        goal=goal,
                        runtime_contract=contract,
                        parent_run_id=predecessor_run_id,
                        predecessor_run_id=predecessor_run_id,
                    )
                )
            except RunStoreError:
                # ``enqueue`` and the supervisor's ``release_ready`` can both
                # decide to start the same queued item.  The run-exists check
                # above is TOCTOU, so a concurrent starter may win the insert;
                # that is a benign duplicate start, not corruption — the event
                # appends below are idempotent.  Anything else is re-raised.
                if not await self.store.run_exists(follow_up_run_id):
                    raise
        # Complete the child stream step-by-step.  If the process died after
        # creating the child but before FollowUpStarted, the next supervisor
        # tick can safely continue instead of treating the queue item as a
        # permanently duplicated run.
        child = await self.store.load_projection(follow_up_run_id)
        if child.goal is not None and child.goal.digest != goal.digest:
            raise ValueError("follow-up id already belongs to another goal")
        if child.runtime_contract_digest is not None and child.runtime_contract_digest != contract.digest:
            raise ValueError("follow-up id already belongs to another runtime contract")
        existing_events = (await self.store.read(follow_up_run_id, limit=100_000)).events
        event_types = {event.event_type for event in existing_events}
        if child.sequence == 0:
            await self._append_new_run(follow_up_run_id, "RunCreated", {"goal": goal.to_dict(), "runtime_contract": contract.to_dict(), "task_protocol_version": 1}, contract)
        child = await self.store.load_projection(follow_up_run_id)
        if "PredecessorHandoffCommitted" not in event_types:
            await self._append_new_run(
                follow_up_run_id,
                "PredecessorHandoffCommitted",
                {
                    "predecessor_run_id": predecessor_run_id,
                    "handoff_artifact": handoff.digest,
                    "authorized_recall": [predecessor_run_id],
                },
                contract,
                artifact_refs=(handoff.digest,),
            )
            event_types.add("PredecessorHandoffCommitted")
        child = await self.store.load_projection(follow_up_run_id)
        if "PlanCreated" not in event_types:
            plan = PlanGraph(
                nodes=(PlanNode(f"follow-up-{follow_up_run_id}", "follow_up"),),
                revision=1,
            )
            await self._append_new_run(
                follow_up_run_id,
                "PlanCreated",
                {"plan": plan.to_dict()},
                contract,
            )
            event_types.add("PlanCreated")
        child = await self.store.load_projection(follow_up_run_id)
        if item.continuation and predecessor.todos:
            # A continuation resumes the existing checklist. The control
            # reason remains in the handoff, never becomes a task title.
            # Seed each ID separately so recovery after a partial write does
            # not drop the rest of the checklist or reset later progress.
            created_ids = {
                str(event.payload.get("todo", {}).get("id", ""))
                for event in existing_events if event.event_type == "TodoCreated"
            }
            for todo in predecessor.todos:
                if todo.todo_id in created_ids:
                    continue
                await self._append_new_run(
                    follow_up_run_id,
                    "TodoCreated",
                    {"todo": {
                        "id": todo.todo_id,
                        "title": todo.title,
                        "status": "done" if todo.status in {"done", "completed"} else "pending",
                        "active_sessions": [follow_up_run_id],
                        "evidence": [evidence.to_dict() for evidence in todo.evidence],
                    }},
                    contract,
                )
        elif "TodoCreated" not in event_types:
            await self._append_new_run(
                follow_up_run_id,
                "TodoCreated",
                {
                    "todo": {
                        "id": f"follow-up-{follow_up_run_id}",
                        "title": goal.objective if item.continuation else message,
                        "status": "pending",
                        "active_sessions": [follow_up_run_id],
                        "plan_node_id": f"follow-up-{follow_up_run_id}",
                    }
                },
                contract,
            )
            event_types.add("TodoCreated")
        child = await self.store.load_projection(follow_up_run_id)
        if "GoalContractAccepted" not in event_types:
            await self._append_new_run(follow_up_run_id, "GoalContractAccepted", {"goal": goal.to_dict()}, contract)
        child = await self.store.load_projection(follow_up_run_id)
        if "RunQueued" not in event_types:
            await self._append_new_run(follow_up_run_id, "RunQueued", {}, contract)
        predecessor = await self.store.load_projection(predecessor_run_id)
        await self.store.append(
            self._event(predecessor, "FollowUpStarted", {"follow_up_run_id": follow_up_run_id}),
            expected_sequence=predecessor.sequence,
        )

    async def _append_new_run(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        contract: RuntimeContract,
        *,
        artifact_refs: tuple[str, ...] = (),
    ) -> None:
        projection = await self.store.load_projection(run_id)
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=self.actor,
            runtime_contract_digest=contract.digest,
            payload=payload,
            artifact_refs=artifact_refs,
        )
        await self.store.append(event, expected_sequence=projection.sequence)

    def _event(self, projection, event_type: str, payload: dict[str, object]) -> RunEvent:
        return RunEvent.create(
            run_id=projection.run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=self.actor,
            runtime_contract_digest=str(projection.runtime_contract_digest),
            payload=payload,
        )

    @staticmethod
    def _find_item(queue: tuple[QueueProjection, ...], follow_up_run_id: str) -> QueueProjection:
        for item in queue:
            if item.follow_up_run_id == follow_up_run_id:
                return item
        raise ValueError(f"unknown follow-up: {follow_up_run_id}")


__all__ = ["FollowUpReceipt", "FollowUpService"]
