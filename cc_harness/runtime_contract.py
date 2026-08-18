"""Pinned runtime contract and safe migration seam."""

from __future__ import annotations

from dataclasses import dataclass

from .run_events import EventActor, RunEvent
from .run_model import RuntimeContract
from .run_store import LeaseFenceError, RunStore


class RuntimeContractMismatch(LeaseFenceError):
    """A worker or event was produced under a different pinned contract."""


@dataclass(frozen=True)
class RuntimeMigrationReceipt:
    run_id: str
    previous_digest: str
    new_digest: str
    sequence: int


class RuntimeContractService:
    def assert_compatible(self, pinned: RuntimeContract, candidate: RuntimeContract) -> None:
        if pinned.digest != candidate.digest:
            raise RuntimeContractMismatch(
                f"runtime contract mismatch: pinned={pinned.digest}, candidate={candidate.digest}"
            )

    async def migrate(
        self,
        store: RunStore,
        run_id: str,
        new_contract: RuntimeContract,
        *,
        reason: str,
        actor: EventActor | None = None,
    ) -> RuntimeMigrationReceipt:
        projection = await store.load_projection(run_id)
        current_lease = await store.current_lease(run_id)
        if current_lease is not None:
            raise RuntimeContractMismatch("runtime migration requires a quiescent run with no active lease")
        previous = str(projection.runtime_contract_digest or "")
        if not previous:
            raise RuntimeContractMismatch("run has no pinned runtime contract")
        if previous == new_contract.digest:
            raise RuntimeContractMismatch("runtime migration must change the pinned contract")
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="RunRuntimeMigrated",
            actor=actor or EventActor("supervisor", "local-supervisor"),
            runtime_contract_digest=previous,
            payload={
                "previous_runtime_contract_digest": previous,
                "new_runtime_contract_digest": new_contract.digest,
                "new_runtime_contract": new_contract.to_dict(),
                "reason": reason,
            },
        )
        await store.append(event, expected_sequence=projection.sequence)
        return RuntimeMigrationReceipt(run_id, previous, new_contract.digest, event.sequence)


__all__ = ["RuntimeContractMismatch", "RuntimeContractService", "RuntimeMigrationReceipt"]
