from __future__ import annotations

import pytest

from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus
from cc_harness.run_store import RunStore
from cc_harness.worker import ActionExecutionResult, RunWorker
from cc_harness.coordinator import RunCoordinator, RunRequest


class FakeModel:
    async def complete(self, messages, tools):
        return ModelSegment(
            text="read",
            tool_calls=({"id": "action-worker", "name": "Read", "arguments": {"path": "README.md"}},),
        )


@pytest.mark.asyncio
async def test_worker_persists_segment_and_action_recovery_facts(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("worker task", ("read",)))
        worker = RunWorker(
            store,
            ReActKernel(FakeModel()),
            worker_id="worker-1",
            action_executor=lambda request: _success(request),
        )
        lease = await worker.claim(handle.run_id)
        await worker.execute(lease)
        events = (await store.read(handle.run_id)).events
        assert [event.event_type for event in events[-5:]] == [
            "ActionPrepared",
            "ActionStarted",
            "ActionSucceeded",
            "RunSegmentFinished",
            "RunYielded",
        ]
        projection = await store.load_projection(handle.run_id)
        assert projection.actions[0].status is ActionStatus.SUCCEEDED
        assert await store.current_lease(handle.run_id) is None
    finally:
        await store.close()


async def _success(request):
    return ActionExecutionResult(ActionStatus.SUCCEEDED, read_paths=("README.md",))
