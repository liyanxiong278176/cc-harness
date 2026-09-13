from __future__ import annotations

import asyncio

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.lease import LeaseManager, ResourceLeaseManager
from cc_harness.run_kernel import ActionRequest, ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus, EffectClass
from cc_harness.run_store import ResourceLeaseConflict, RunStore, SupervisorLeaseConflict
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import ActionExecutionResult, RunWorker


class _IdleModel:
    async def complete(self, messages, tools):
        del messages, tools
        return ModelSegment(text="waiting")


async def _open_stores(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data_root = tmp_path / "state"
    first = await RunStore(project, data_root=data_root).open()
    second = await RunStore(project, data_root=data_root).open()
    return first, second


@pytest.mark.asyncio
async def test_project_supervisor_lease_is_atomic_and_fenced(tmp_path) -> None:
    first, second = await _open_stores(tmp_path)
    try:
        leader = await first.claim_supervisor_lease("supervisor-a", ttl_seconds=10)
        with pytest.raises(SupervisorLeaseConflict):
            await second.claim_supervisor_lease("supervisor-b", ttl_seconds=10)

        renewed = await first.heartbeat_supervisor_lease(leader, ttl_seconds=10)
        assert renewed.epoch == leader.epoch
        assert renewed.expires_at > leader.expires_at - 1
        assert await first.release_supervisor_lease(renewed)

        replacement = await second.claim_supervisor_lease("supervisor-b", ttl_seconds=10)
        # A clean release removes the active row; a later acquisition starts a
        # fresh leadership epoch.  Expiry-based takeover (without release)
        # advances the persisted epoch and is covered by the fencing logic.
        assert replacement.epoch == 1
        # The old owner cannot delete the replacement lease after takeover.
        assert not await first.release_supervisor_lease(leader)
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_resource_leases_allow_shared_reads_and_fence_overlapping_writes(tmp_path) -> None:
    store, _unused = await _open_stores(tmp_path)
    try:
        coordinator = RunCoordinator(store)
        first = await coordinator.submit(RunRequest("first", ("done",)))
        second = await coordinator.submit(RunRequest("second", ("done",)))
        first_lease = await LeaseManager(store).claim(first.run_id, "worker-a")
        second_lease = await LeaseManager(store).claim(second.run_id, "worker-b")

        manager = ResourceLeaseManager(store, ttl_seconds=10)
        first_resource = await manager.acquire_for_action(
            first_lease,
            ActionRequest("write-a", "Write", {"path": "src/app.py"}),
            effect="workspace_mutation",
            working_directory=store.project_root,
        )
        with pytest.raises(ResourceLeaseConflict):
            await store.claim_resources(
                second.run_id,
                second_lease.epoch,
                ((first_resource[0].resource_key, "exclusive"),),
                ttl_seconds=10,
                action_id="write-b",
            )

        await manager.release(first_resource)
        shared_a = await store.claim_resources(
            first.run_id,
            first_lease.epoch,
            (("path:/tmp/shared", "shared"),),
            ttl_seconds=10,
            action_id="read-a",
        )
        shared_b = await store.claim_resources(
            second.run_id,
            second_lease.epoch,
            (("path:/tmp/shared", "shared"),),
            ttl_seconds=10,
            action_id="read-b",
        )
        assert len(shared_a) == len(shared_b) == 1
        await store.release_resources(shared_a + shared_b)
    finally:
        await store.close()
        await _unused.close()


@pytest.mark.asyncio
async def test_supervisor_does_not_serialize_independent_root_runs(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "state").open()
    stopped = asyncio.Event()

    class WaitingWorker(RunWorker):
        async def execute(self, lease):
            del lease
            await stopped.wait()

    try:
        coordinator = RunCoordinator(store)
        first = await coordinator.submit(RunRequest("first", ("done",)))
        second = await coordinator.submit(RunRequest("second", ("done",)))

        def factory(run_id):
            return WaitingWorker(
                store,
                ReActKernel(_IdleModel()),
                worker_id=f"worker-{run_id}",
            )

        supervisor = LocalSupervisor(store, factory, max_workers=2, poll_interval=0.01)
        await supervisor.tick()
        assert set(supervisor._active) == {first.run_id, second.run_id}
        stopped.set()
        await supervisor.stop(drain=False)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_worker_resource_scope_serializes_same_file_actions(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "state").open()
    in_flight = 0
    peak = 0

    async def executor(_request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.1)
        in_flight -= 1
        return ActionExecutionResult(
            ActionStatus.SUCCEEDED,
            modified_paths=("src/app.py",),
        )

    try:
        coordinator = RunCoordinator(store)
        first = await coordinator.submit(RunRequest("first", ("done",)))
        second = await coordinator.submit(RunRequest("second", ("done",)))
        first_lease = await LeaseManager(store, ttl_seconds=10).claim(first.run_id, "worker-a")
        second_lease = await LeaseManager(store, ttl_seconds=10).claim(second.run_id, "worker-b")
        resources = ResourceLeaseManager(store, ttl_seconds=10, poll_interval=0.01)
        worker_a = RunWorker(
            store,
            ReActKernel(_IdleModel()),
            worker_id="worker-a",
            resource_manager=resources,
            action_executor=executor,
        )
        worker_b = RunWorker(
            store,
            ReActKernel(_IdleModel()),
            worker_id="worker-b",
            resource_manager=resources,
            action_executor=executor,
        )
        request_a = ActionRequest(
            "write-a",
            "Write",
            {"path": "src/app.py", "content": "a"},
            effect_class=EffectClass.WORKSPACE_MUTATION,
        )
        request_b = ActionRequest(
            "write-b",
            "Write",
            {"path": "src/app.py", "content": "b"},
            effect_class=EffectClass.WORKSPACE_MUTATION,
        )
        statuses = await asyncio.gather(
            worker_a._execute_action(first_lease, request_a),
            worker_b._execute_action(second_lease, request_b),
        )
        assert statuses == [ActionStatus.SUCCEEDED, ActionStatus.SUCCEEDED]
        assert peak == 1
        assert await store.list_resource_leases(include_expired=True) == ()
        await store.release_lease(first.run_id, first_lease.epoch)
        await store.release_lease(second.run_id, second_lease.epoch)
    finally:
        await store.close()
