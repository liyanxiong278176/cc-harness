import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import RuntimeContract
from cc_harness.run_store import LeaseFenceError, RunStore
from cc_harness.runtime_contract import RuntimeContractMismatch, RuntimeContractService


def _new_contract() -> RuntimeContract:
    return RuntimeContract("runtime-rebuild-v2", 1, "sha256:tools2", "sha256:model", "sha256:policy", "sha256:cap")


@pytest.mark.asyncio
async def test_migration_pins_new_contract_and_fences_old_events(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("contract task", ("done",)))
        view = await RunCoordinator(store).inspect(handle.run_id)
        old_digest = view.projection.runtime_contract_digest
        new_contract = _new_contract()
        receipt = await RuntimeContractService().migrate(store, handle.run_id, new_contract, reason="upgrade")
        assert receipt.previous_digest == old_digest
        assert (await store.load_projection(handle.run_id)).runtime_contract_digest == new_contract.digest
        projection = await store.load_projection(handle.run_id)
        stale = RunEvent.create(
            run_id=handle.run_id,
            sequence=projection.sequence + 1,
            event_type="FollowUpQueued",
            actor=EventActor("worker", "old-worker"),
            runtime_contract_digest=str(old_digest),
            lease_epoch=0,
            payload={"follow_up_run_id": "not-a-uuid", "message_artifact": "sha256:" + "0" * 64},
        )
        with pytest.raises(LeaseFenceError):
            await store.append(stale, expected_sequence=projection.sequence)
    finally:
        await store.close()


def test_contract_service_rejects_mismatch() -> None:
    first = RuntimeContract("v1", 1, "tools", "model", "policy", "cap")
    second = RuntimeContract("v2", 1, "tools", "model", "policy", "cap")
    with pytest.raises(RuntimeContractMismatch):
        RuntimeContractService().assert_compatible(first, second)
