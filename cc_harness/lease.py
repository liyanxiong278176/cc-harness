"""Durable worker lease lifecycle and fencing."""

from __future__ import annotations

import time

from .run_events import EventActor, RunEvent
from .run_model import Lease, RunStatus
from .run_store import LeaseFenceError, RunStore


class LeaseManager:
    def __init__(self, store: RunStore, *, ttl_seconds: float = 30.0) -> None:
        self.store = store
        self.ttl_seconds = max(1.0, ttl_seconds)

    async def claim(self, run_id: str, worker_id: str) -> Lease:
        projection = await self.store.load_projection(run_id)
        if projection.status is not RunStatus.QUEUED:
            raise LeaseFenceError(f"run is not claimable: {projection.status.value}")
        current = await self.store.current_lease(run_id)
        current_epoch = current.epoch if current is not None else projection.lease_epoch
        epoch = current_epoch + 1
        expires_at = time.time() + self.ttl_seconds
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="RunClaimed",
            actor=EventActor("worker", worker_id),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=epoch,
            payload={"worker_id": worker_id, "expires_at": expires_at},
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=current_epoch,
        )
        return Lease(run_id, worker_id, epoch, expires_at - self.ttl_seconds, expires_at)

    async def heartbeat(self, lease: Lease) -> Lease:
        if lease.is_expired():
            raise LeaseFenceError("cannot heartbeat an expired lease")
        projection = await self.store.load_projection(lease.run_id)
        current = await self.store.current_lease(lease.run_id)
        if current is None or current.epoch != lease.epoch or current.worker_id != lease.worker_id:
            raise LeaseFenceError("lease is no longer current")
        expires_at = time.time() + self.ttl_seconds
        event = RunEvent.create(
            run_id=lease.run_id,
            sequence=projection.sequence + 1,
            event_type="WorkerHeartbeat",
            actor=EventActor("worker", lease.worker_id),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=lease.epoch,
            payload={"heartbeat_at": str(time.time()), "expires_at": expires_at},
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=lease.epoch,
        )
        return Lease(lease.run_id, lease.worker_id, lease.epoch, lease.acquired_at, expires_at)

    async def release(self, lease: Lease) -> bool:
        return await self.store.release_lease(lease.run_id, lease.epoch)

    async def reclaim_expired(self, run_id: str, *, reason: str = "worker lease expired") -> Lease | None:
        current = await self.store.current_lease(run_id)
        if current is None or not current.is_expired():
            return None
        projection = await self.store.load_projection(run_id)
        event_type = "RunCancelled" if projection.status is RunStatus.CANCEL_REQUESTED else "WorkerLeaseExpired"
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=EventActor("supervisor", "local-supervisor"),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=current.epoch,
            payload={"reason": reason, "worker_id": current.worker_id},
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=current.epoch,
        )
        await self.store.release_lease(run_id, current.epoch)
        return current


__all__ = ["LeaseManager"]
