from __future__ import annotations

import pytest

from cc_harness.approvals import ApprovalService
from cc_harness.run_events import EventActor, RunEvent
from cc_harness.run_model import GoalContract, Run, RuntimeContract
from cc_harness.run_store import RunStore


RUN_ID = "00000000-0000-0000-0000-000000000501"
CONTRACT = RuntimeContract("approval-test", 1, "sha256:tools", "sha256:model", "sha256:policy", "sha256:cap")
GOAL = GoalContract("approve action", ("approval persists",))


async def active_store(tmp_path) -> RunStore:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    await store.create_run(Run(RUN_ID, GOAL, CONTRACT))
    actor = EventActor("user", "user-1")
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
    await store.append(
        RunEvent.create(
            run_id=RUN_ID,
            sequence=3,
            event_type="RunClaimed",
            actor=EventActor("worker", "worker-1"),
            runtime_contract_digest=CONTRACT.digest,
            payload={"worker_id": "worker-1"},
            lease_epoch=1,
        ),
        expected_sequence=2,
        expected_lease_epoch=0,
    )
    return store


@pytest.mark.asyncio
async def test_approval_is_persisted_and_worker_lease_is_released(tmp_path) -> None:
    store = await active_store(tmp_path)
    try:
        service = ApprovalService(store)
        request = await service.request(
            run_id=RUN_ID,
            action_id="action-1",
            action_args_digest="sha256:args",
            scope=("network",),
            actor=EventActor("worker", "worker-1"),
            lease_epoch=1,
        )
        assert request.status.value == "requested"
        assert (await store.load_projection(RUN_ID)).status.value == "awaiting_approval"
        decision = await service.grant(
            run_id=RUN_ID,
            approval_id=request.approval_id,
            action_args_digest="sha256:args",
            actor=EventActor("user", "user-1"),
        )
        assert decision.status == "granted"
        assert (await store.load_projection(RUN_ID)).status.value == "queued"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parameter_change_cannot_use_existing_approval(tmp_path) -> None:
    store = await active_store(tmp_path)
    try:
        service = ApprovalService(store)
        request = await service.request(
            run_id=RUN_ID,
            action_id="action-1",
            action_args_digest="sha256:args",
            scope=("network",),
            actor=EventActor("worker", "worker-1"),
            lease_epoch=1,
        )
        with pytest.raises(Exception):
            await service.grant(
                run_id=RUN_ID,
                approval_id=request.approval_id,
                action_args_digest="sha256:changed",
                actor=EventActor("user", "user-1"),
            )
        assert (await store.load_projection(RUN_ID)).status.value == "awaiting_approval"
    finally:
        await store.close()
