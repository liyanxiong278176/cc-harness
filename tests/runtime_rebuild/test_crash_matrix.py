import asyncio

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.lease import LeaseManager
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_store import LeaseFenceError, RunStore


async def _claim(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data_root = tmp_path / "data"
    store = RunStore(project, data_root=data_root)
    await store.open()
    handle = await RunCoordinator(store).submit(RunRequest("crash matrix", ("done",)))
    lease = await LeaseManager(store, ttl_seconds=1).claim(handle.run_id, "worker-crash")
    return project, data_root, store, handle.run_id, lease


@pytest.mark.asyncio
async def test_restart_after_started_preserves_unknown_without_replay(tmp_path) -> None:
    project, data_root, store, run_id, lease = await _claim(tmp_path)
    try:
        projection = await store.load_projection(run_id)
        actor = EventActor("worker", lease.worker_id)
        for offset, event_type, payload in (
            (1, "ActionPlanned", {"action_id": "crash-action", "attempt": 1, "tool_name": "run_command", "effect_class": "external_side_effect"}),
            (2, "ActionPrepared", {"action_id": "crash-action", "attempt": 1}),
            (3, "ActionStarted", {"action_id": "crash-action", "attempt": 1}),
        ):
            current = await store.load_projection(run_id)
            await store.append(
                RunEvent.create(
                    run_id=run_id,
                    sequence=current.sequence + 1,
                    event_type=event_type,
                    actor=actor,
                    runtime_contract_digest=str(current.runtime_contract_digest),
                    lease_epoch=lease.epoch,
                    payload=payload,
                ),
                expected_sequence=current.sequence,
                expected_lease_epoch=lease.epoch,
            )
        await store.close()
    finally:
        if store._db is not None:
            await store.close()
    restarted = RunStore(project, data_root=data_root)
    await restarted.open()
    try:
        projection = await restarted.load_projection(run_id)
        assert projection.actions[0].status.value == "started"
        assert await restarted.current_lease(run_id) is not None
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_expired_lease_fences_old_worker_after_reclaim(tmp_path) -> None:
    _project, _data_root, store, run_id, lease = await _claim(tmp_path)
    try:
        await asyncio.sleep(1.05)
        await LeaseManager(store, ttl_seconds=1).reclaim_expired(run_id)
        projection = await store.load_projection(run_id)
        stale = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="WorkerHeartbeat",
            actor=EventActor("worker", lease.worker_id),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=lease.epoch,
            payload={"heartbeat_at": "2026-08-18T00:00:00Z", "expires_at": 4102444800.0},
        )
        with pytest.raises(LeaseFenceError):
            await store.append(stale, expected_sequence=projection.sequence, expected_lease_epoch=lease.epoch)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_lease_finishes_a_cancel_requested_run(tmp_path) -> None:
    _project, _data_root, store, run_id, lease = await _claim(tmp_path)
    try:
        projection = await store.load_projection(run_id)
        await store.append(
            RunEvent.create(
                run_id=run_id,
                sequence=projection.sequence + 1,
                event_type="InterruptRequested",
                actor=EventActor("client", "local-client"),
                runtime_contract_digest=str(projection.runtime_contract_digest),
                payload={"reason": "crash test"},
            ),
            expected_sequence=projection.sequence,
        )
        await asyncio.sleep(1.05)
        await LeaseManager(store, ttl_seconds=1).reclaim_expired(run_id)
        assert (await store.load_projection(run_id)).status.value == "cancelled"
    finally:
        await store.close()
