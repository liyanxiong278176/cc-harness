from __future__ import annotations

import asyncio

import pytest

from cc_harness.lease import LeaseManager
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import GoalContract, Run, RuntimeContract
from cc_harness.run_store import RunStore


RUN_ID = "00000000-0000-0000-0000-000000000601"
CONTRACT = RuntimeContract("lease-test", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap")
GOAL = GoalContract("lease", ("reclaim safely",))


async def queued_store(tmp_path) -> RunStore:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    await store.create_run(Run(RUN_ID, GOAL, CONTRACT))
    actor = EventActor("user", "user")
    await store.append(
        RunEvent.create(
            run_id=RUN_ID,
            sequence=1,
            event_type="RunCreated",
            actor=actor,
            runtime_contract_digest=CONTRACT.digest,
            payload={"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()},
        ),
        expected_sequence=0,
    )
    await store.append(
        RunEvent.create(
            run_id=RUN_ID,
            sequence=2,
            event_type="RunQueued",
            actor=actor,
            runtime_contract_digest=CONTRACT.digest,
            payload={},
        ),
        expected_sequence=1,
    )
    return store


@pytest.mark.asyncio
async def test_claim_heartbeat_release_and_reclaim_are_fenced(tmp_path) -> None:
    store = await queued_store(tmp_path)
    try:
        manager = LeaseManager(store, ttl_seconds=1)
        lease = await manager.claim(RUN_ID, "worker-1")
        assert lease.epoch == 1
        renewed = await manager.heartbeat(lease)
        assert renewed.expires_at > lease.expires_at
        assert await manager.release(renewed)
        assert await store.current_lease(RUN_ID) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_lease_recovery_returns_run_to_queue(tmp_path) -> None:
    store = await queued_store(tmp_path)
    try:
        manager = LeaseManager(store, ttl_seconds=1)
        await manager.claim(RUN_ID, "worker-1")
        await asyncio.sleep(1.05)
        reclaimed = await manager.reclaim_expired(RUN_ID)
        assert reclaimed is not None
        assert (await store.load_projection(RUN_ID)).status.value == "queued"
        assert await store.current_lease(RUN_ID) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_running_projection_without_lease_is_recovered(tmp_path) -> None:
    store = await queued_store(tmp_path)
    try:
        manager = LeaseManager(store, ttl_seconds=1)
        lease = await manager.claim(RUN_ID, "worker-crash")
        assert await store.release_lease(RUN_ID, lease.epoch)
        assert await manager.reclaim_expired(RUN_ID) is None
        projection = await store.load_projection(RUN_ID)
        assert projection.status.value == "queued"
        assert projection.active_worker_id is None
        events = (await store.read(RUN_ID, limit=100)).events
        assert events[-1].event_type == "WorkerLeaseExpired"
        assert "no active worker lease" in str(events[-1].payload["reason"])
    finally:
        await store.close()
