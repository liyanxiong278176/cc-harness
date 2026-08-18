"""Persistent approval service backed by RunStore events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .run_events import EventActor, RunEvent
from .run_projection import ApprovalProjection
from .run_store import RunStore


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    status: str
    run_id: str


class ApprovalService:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    async def request(
        self,
        *,
        run_id: str,
        action_id: str,
        action_args_digest: str,
        scope: tuple[str, ...],
        actor: EventActor,
        lease_epoch: int,
    ) -> ApprovalProjection:
        projection = await self.store.load_projection(run_id)
        approval_id = str(uuid.uuid4())
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="ApprovalRequested",
            actor=actor,
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=lease_epoch,
            payload={
                "approval_id": approval_id,
                "action_id": action_id,
                "action_args_digest": action_args_digest,
                "scope": list(scope),
            },
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=lease_epoch,
        )
        updated = await self.store.load_projection(run_id)
        return next(item for item in updated.approvals if item.approval_id == approval_id)

    async def grant(
        self,
        *,
        run_id: str,
        approval_id: str,
        action_args_digest: str,
        actor: EventActor,
    ) -> ApprovalDecision:
        return await self._decide(
            run_id=run_id,
            approval_id=approval_id,
            actor=actor,
            event_type="ApprovalGranted",
            payload={"approval_id": approval_id, "action_args_digest": action_args_digest},
        )

    async def reject(
        self,
        *,
        run_id: str,
        approval_id: str,
        reason: str,
        actor: EventActor,
    ) -> ApprovalDecision:
        return await self._decide(
            run_id=run_id,
            approval_id=approval_id,
            actor=actor,
            event_type="ApprovalRejected",
            payload={"approval_id": approval_id, "reason": reason},
        )

    async def _decide(
        self,
        *,
        run_id: str,
        approval_id: str,
        actor: EventActor,
        event_type: str,
        payload: dict,
    ) -> ApprovalDecision:
        projection = await self.store.load_projection(run_id)
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=actor,
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=0,
            payload=payload,
        )
        await self.store.append(event, expected_sequence=projection.sequence)
        updated = await self.store.load_projection(run_id)
        approval = next(item for item in updated.approvals if item.approval_id == approval_id)
        return ApprovalDecision(approval_id, approval.status.value, run_id)


__all__ = ["ApprovalDecision", "ApprovalService"]
