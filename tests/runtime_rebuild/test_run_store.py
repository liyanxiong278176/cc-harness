from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import GoalContract, Run, RuntimeContract
from cc_harness.run_store import LeaseFenceError, RunStore, SequenceConflict


RUN_ID = "00000000-0000-0000-0000-000000000201"
ACTOR = EventActor("worker", "worker-1")
CONTRACT = RuntimeContract("store-test", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap")
GOAL = GoalContract("store a run", ("events persist",))


def event(sequence: int, event_type: str, payload: dict, *, lease_epoch: int = 0, digest: str | None = None):
    return RunEvent.create(
        event_id=f"00000000-0000-0000-0000-{sequence:012d}",
        run_id=RUN_ID,
        sequence=sequence,
        event_type=event_type,
        actor=ACTOR,
        runtime_contract_digest=digest or CONTRACT.digest,
        payload=payload,
        lease_epoch=lease_epoch,
        correlation_id="00000000-0000-0000-0000-000000000299",
    )


async def opened_store(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "user-data")
    await store.open()
    await store.create_run(Run(RUN_ID, GOAL, CONTRACT))
    return store


async def append_created(store: RunStore) -> None:
    await store.append(
        event(1, "RunCreated", {"goal": GOAL.to_dict(), "runtime_contract": CONTRACT.to_dict()}),
        expected_sequence=0,
    )


@pytest.mark.asyncio
async def test_append_read_and_projection_are_transactionally_consistent(tmp_path) -> None:
    store = await opened_store(tmp_path)
    try:
        await append_created(store)
        await store.append(event(2, "RunQueued", {}), expected_sequence=1)
        await store.append(
            event(3, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1),
            expected_sequence=2,
            expected_lease_epoch=0,
        )
        await store.append(
            event(
                4,
                "WorkerHeartbeat",
                {"heartbeat_at": "2026-08-18T00:00:00Z", "expires_at": 4102444800.0},
                lease_epoch=1,
            ),
            expected_sequence=3,
            expected_lease_epoch=1,
        )

        page = await store.read(RUN_ID)
        projection = await store.load_projection(RUN_ID)
        assert [item.event_type for item in page.events] == [
            "RunCreated",
            "RunQueued",
            "RunClaimed",
            "WorkerHeartbeat",
        ]
        assert projection.status.value == "running"
        assert projection.sequence == 4
        assert projection.active_worker_id == "worker-1"
        assert projection.lease_epoch == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sequence_conflict_rolls_back_without_partial_event(tmp_path) -> None:
    store = await opened_store(tmp_path)
    try:
        await append_created(store)
        with pytest.raises(SequenceConflict):
            await store.append(event(9, "RunQueued", {}), expected_sequence=0)
        page = await store.read(RUN_ID)
        assert len(page.events) == 1
        assert page.events[0].sequence == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lease_epoch_fences_old_worker_and_runtime_migration_fences_old_contract(tmp_path) -> None:
    store = await opened_store(tmp_path)
    try:
        await append_created(store)
        await store.append(event(2, "RunQueued", {}), expected_sequence=1)
        await store.append(
            event(3, "RunClaimed", {"worker_id": "worker-1"}, lease_epoch=1),
            expected_sequence=2,
            expected_lease_epoch=0,
        )
        with pytest.raises(LeaseFenceError):
            await store.append(
                event(4, "WorkerHeartbeat", {"heartbeat_at": "2026-08-18T00:00:00Z"}),
                expected_sequence=3,
                expected_lease_epoch=0,
            )

        new_digest = "sha256:new-runtime-contract"
        await store.append(
            event(
                4,
                "RunRuntimeMigrated",
                {
                    "previous_runtime_contract_digest": CONTRACT.digest,
                    "new_runtime_contract_digest": new_digest,
                },
                lease_epoch=1,
                digest=new_digest,
            ),
            expected_sequence=3,
            expected_lease_epoch=1,
        )
        with pytest.raises(LeaseFenceError):
            await store.append(
                event(
                    5,
                    "WorkerHeartbeat",
                    {"heartbeat_at": "2026-08-18T00:00:01Z"},
                    lease_epoch=1,
                    digest=CONTRACT.digest,
                ),
                expected_sequence=4,
                expected_lease_epoch=1,
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_snapshot_survives_restart_and_is_only_an_accelerator(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data_root = tmp_path / "user-data"
    store = RunStore(project, data_root=data_root)
    await store.open()
    await store.create_run(Run(RUN_ID, GOAL, CONTRACT))
    await append_created(store)
    await store.append(event(2, "RunQueued", {}), expected_sequence=1)
    snapshot = await store.load_projection(RUN_ID)
    await store.save_snapshot(snapshot)
    await store.close()

    restarted = RunStore(project, data_root=data_root)
    await restarted.open()
    try:
        assert await restarted.snapshot_sequences(RUN_ID) == (2,)
        assert (await restarted.load_projection(RUN_ID)).digest == snapshot.digest
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_event_and_snapshot_immutability_triggers_are_enforced(tmp_path) -> None:
    store = await opened_store(tmp_path)
    await append_created(store)
    await store.save_snapshot(await store.load_projection(RUN_ID))
    await store.close()
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "UPDATE run_event SET event_type = 'tampered' WHERE run_id = ? AND sequence = 1",
                (RUN_ID,),
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("DELETE FROM run_event WHERE run_id = ? AND sequence = 1", (RUN_ID,))
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "DELETE FROM run_snapshot WHERE run_id = ? AND sequence = 1", (RUN_ID,)
            )


@pytest.mark.asyncio
async def test_readers_can_share_single_store_and_identity_is_stable(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "user-data")
    await store.open()
    await store.create_run(Run(RUN_ID, GOAL, CONTRACT))
    await append_created(store)
    try:
        pages = await asyncio.gather(*(store.read(RUN_ID) for _ in range(8)))
        assert all(len(page.events) == 1 for page in pages)
    finally:
        await store.close()

    worktree = tmp_path / "child-worktree"
    worktree.mkdir()
    main_store = RunStore(project, data_root=tmp_path / "shared", identity_root=project)
    child_store = RunStore(worktree, data_root=tmp_path / "shared", identity_root=project)
    assert main_store.db_path == child_store.db_path


@pytest.mark.asyncio
async def test_derived_tables_and_snapshot_metadata_commit_with_event(tmp_path) -> None:
    store = await opened_store(tmp_path)
    try:
        await append_created(store)
        await store.append(event(2, "RunQueued", {}), expected_sequence=1)
        await store.append(
            event(
                3,
                "RunClaimed",
                {"worker_id": "worker-1"},
                lease_epoch=1,
            ),
            expected_sequence=2,
            expected_lease_epoch=0,
        )
        await store.append(
            event(
                4,
                "ActionPlanned",
                {
                    "action_id": "action-1",
                    "attempt": 1,
                    "tool_name": "Read",
                    "effect_class": "read_only",
                    "normalized_args_digest": "sha256:args",
                    "contract_digest": "sha256:contract",
                },
                lease_epoch=1,
            ),
            expected_sequence=3,
            expected_lease_epoch=1,
            snapshot=None,
        )
        async with aiosqlite.connect(store.db_path) as db:
            cursor = await db.execute(
                "SELECT status FROM action_attempt WHERE run_id = ? AND action_id = ?",
                (RUN_ID, "action-1"),
            )
            action = await cursor.fetchone()
            assert action[0] == "planned"
    finally:
        await store.close()
