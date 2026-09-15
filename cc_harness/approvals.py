"""Persistent approval service backed by RunStore events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .run_events import EventActor, RunEvent
from .run_model import ApprovalStatus
from .run_projection import ApprovalProjection, ProjectionError
from .run_store import RunStore, RunStoreError, SequenceConflict


class ApprovalError(RunStoreError):
    """Base error for a durable approval decision."""


class ApprovalNotFoundError(ApprovalError):
    """The requested approval is not part of the run's durable stream."""


class ApprovalDigestMismatchError(ProjectionError):
    """A decision attempted to use arguments different from the request."""


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
        # A browser can submit the same decision more than once (double click,
        # reconnect, or two windows).  Resolve the approval from the durable
        # projection on every attempt and make terminal decisions idempotent.
        # This avoids turning a harmless stale card into a 500/400 and keeps
        # the immutable event stream at exactly one decision event.
        for _attempt in range(3):
            projection = await self.store.load_projection(run_id)
            approval = next(
                (item for item in projection.approvals if item.approval_id == approval_id),
                None,
            )
            if approval is None:
                raise ApprovalNotFoundError(
                    f"approval {approval_id} does not belong to run {run_id}"
                )
            if approval.status is not ApprovalStatus.REQUESTED:
                return ApprovalDecision(approval_id, approval.status.value, run_id)
            if event_type == "ApprovalGranted":
                requested_digest = str(payload.get("action_args_digest") or "")
                if requested_digest != approval.action_args_digest:
                    raise ApprovalDigestMismatchError(
                        "approval parameters changed after request"
                    )
            event = RunEvent.create(
                run_id=run_id,
                sequence=projection.sequence + 1,
                event_type=event_type,
                actor=actor,
                runtime_contract_digest=str(projection.runtime_contract_digest),
                lease_epoch=0,
                payload=payload,
            )
            try:
                await self.store.append(event, expected_sequence=projection.sequence)
            except SequenceConflict:
                # Another process may have decided the approval between the
                # read and append.  Rebuild and return its terminal decision;
                # if the conflict was an unrelated event, the next iteration
                # retries against the new sequence.
                continue
            updated = await self.store.load_projection(run_id)
            decided = next(
                (item for item in updated.approvals if item.approval_id == approval_id),
                None,
            )
            if decided is None:
                raise ApprovalNotFoundError(
                    f"approval {approval_id} disappeared from run {run_id}"
                )
            return ApprovalDecision(approval_id, decided.status.value, run_id)
        raise SequenceConflict("approval decision raced with another durable update")


__all__ = [
    "ApprovalDecision",
    "ApprovalDigestMismatchError",
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalService",
]
