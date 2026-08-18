"""Durable ordinary-message queue and predecessor gate handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .artifacts import ArtifactStore
from .run_events import EventActor, RunEvent
from .run_model import GoalContract, Run, RuntimeContract, predecessor_gate
from .run_projection import QueueProjection
from .run_store import RunStore


@dataclass(frozen=True)
class FollowUpReceipt:
    predecessor_run_id: str
    follow_up_run_id: str
    sequence: int
    gate: str
    started: bool


class FollowUpService:
    """Queue normal messages and materialize each as its own Run."""

    def __init__(self, store: RunStore, *, artifacts: ArtifactStore | None = None) -> None:
        self.store = store
        self.artifacts = artifacts or store.artifacts
        self.actor = EventActor("client", "local-client")

    async def enqueue(self, predecessor_run_id: str, message: str) -> FollowUpReceipt:
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
            },
        )
        await self.store.append(event, expected_sequence=predecessor.sequence)
        started = gate in {"ready", "incomplete"}
        if started:
            await self._start(predecessor_run_id, follow_up_run_id, artifact.digest, incomplete=gate == "incomplete")
        return FollowUpReceipt(predecessor_run_id, follow_up_run_id, event.sequence, gate, started)

    async def release_ready(self, predecessor_run_id: str) -> tuple[FollowUpReceipt, ...]:
        predecessor = await self.store.load_projection(predecessor_run_id)
        ready: list[FollowUpReceipt] = []
        for item in predecessor.queue:
            if item.status != "queued":
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
        return tuple(ready)

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

    async def _start(
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
        )
        if not await self.store.run_exists(follow_up_run_id):
            await self.store.create_run(
                Run(
                    run_id=follow_up_run_id,
                    goal=goal,
                    runtime_contract=contract,
                    parent_run_id=predecessor_run_id,
                    predecessor_run_id=predecessor_run_id,
                )
            )
        # Complete the child stream step-by-step.  If the process died after
        # creating the child but before FollowUpStarted, the next supervisor
        # tick can safely continue instead of treating the queue item as a
        # permanently duplicated run.
        child = await self.store.load_projection(follow_up_run_id)
        if child.goal is not None and child.goal.digest != goal.digest:
            raise ValueError("follow-up id already belongs to another goal")
        if child.runtime_contract_digest is not None and child.runtime_contract_digest != contract.digest:
            raise ValueError("follow-up id already belongs to another runtime contract")
        if child.sequence == 0:
            await self._append_new_run(follow_up_run_id, "RunCreated", {"goal": goal.to_dict(), "runtime_contract": contract.to_dict()}, contract)
        child = await self.store.load_projection(follow_up_run_id)
        if child.sequence == 1:
            await self._append_new_run(follow_up_run_id, "GoalContractAccepted", {"goal": goal.to_dict()}, contract)
        child = await self.store.load_projection(follow_up_run_id)
        if child.sequence == 2:
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
    ) -> None:
        projection = await self.store.load_projection(run_id)
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=self.actor,
            runtime_contract_digest=contract.digest,
            payload=payload,
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
