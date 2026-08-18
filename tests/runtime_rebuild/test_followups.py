import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.followups import FollowUpService
from cc_harness.run_store import RunStore
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import RunWorker


class EmptyModel:
    async def complete(self, messages, tools):
        del messages, tools
        return ModelSegment(text="segment")


@pytest.mark.asyncio
async def test_follow_up_is_queued_then_materialized_after_cancelled_predecessor(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        predecessor = await coordinator.submit(RunRequest("first task", ("done",)))
        queued = await coordinator.send(predecessor.run_id, "continue with the next task")
        assert queued.follow_up_run_id
        assert (await store.load_projection(predecessor.run_id)).queue[0].gate == "waiting"
        await coordinator.cancel(predecessor.run_id, "user cancelled")
        released = await FollowUpService(store).release_ready(predecessor.run_id)
        assert released[0].gate == "incomplete"
        follow_up = await coordinator.inspect(queued.follow_up_run_id)
        assert follow_up.status.value == "queued"
        assert "predecessor_outcome_may_be_incomplete" in follow_up.projection.goal.constraints
        assert (await store.load_projection(predecessor.run_id)).queue[0].status == "started"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_supervisor_releases_follow_up_after_predecessor_cancel(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    supervisor = None
    try:
        coordinator = RunCoordinator(store)
        predecessor = await coordinator.submit(RunRequest("first task", ("done",)))
        queued = await coordinator.send(predecessor.run_id, "continue automatically")
        await coordinator.cancel(predecessor.run_id, "user cancelled")

        def factory(_run_id):
            return RunWorker(store, ReActKernel(EmptyModel()), worker_id="follow-up-worker")

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        await supervisor.tick()
        follow_up = await coordinator.inspect(queued.follow_up_run_id)
        assert follow_up.status.value in {"queued", "running"}
        assert (await store.load_projection(predecessor.run_id)).queue[0].status == "started"
    finally:
        if supervisor is not None:
            await supervisor.stop()
        await store.close()
