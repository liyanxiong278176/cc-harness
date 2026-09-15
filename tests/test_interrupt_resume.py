from __future__ import annotations

import asyncio

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import RunStatus
from cc_harness.run_store import RunStore
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import RunWorker


class InterruptThenCompleteModel:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        return ModelSegment(
            text="已从检查点继续",
            completion_candidate={
                "acceptance_criteria": ["resumed"],
                "evidence": [
                    {
                        "evidence_id": "interrupt-resume-test",
                        "kind": "test",
                        "digest": "sha256:" + "a" * 64,
                        "source": "pytest interrupt resume",
                        "recorded_at": 0.0,
                        "confidence": 1.0,
                    }
                ],
            },
        )


async def _wait_status(coordinator: RunCoordinator, run_id: str, expected: RunStatus) -> None:
    for _ in range(200):
        if (await coordinator.inspect(run_id)).status is expected:
            return
        await asyncio.sleep(0.01)
    actual = (await coordinator.inspect(run_id)).status
    raise AssertionError(f"expected {expected.value}, got {actual.value}")


@pytest.mark.asyncio
async def test_ctrl_c_boundary_can_resume_same_run_from_checkpoint(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "state").open()
    model = InterruptThenCompleteModel()
    coordinator = RunCoordinator(store)
    handle = await coordinator.submit(RunRequest("interrupt and resume", ("resumed",)))

    def factory(_run_id: str) -> RunWorker:
        return RunWorker(
            store,
            ReActKernel(model),
            worker_id="interrupt-resume-worker",
            model_timeout_seconds=60,
            # The integration test focuses on Ctrl+C/checkpoint continuity;
            # production workers still use the durable evidence gate.
            completion_verifier=lambda _candidate: True,
        )

    supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
    try:
        await supervisor.start()
        await asyncio.wait_for(model.started.wait(), timeout=1)

        requested = await coordinator.interrupt(handle.run_id, "Ctrl+C")
        assert requested.status is RunStatus.CANCEL_REQUESTED
        await _wait_status(coordinator, handle.run_id, RunStatus.CANCELLED)

        # Natural-language continuation and the explicit resume endpoint both
        # append RunResumed; the existing run id/checkpoint is reused.
        resumed = await coordinator.resume(handle.run_id, "继续")
        assert resumed.run_id == handle.run_id
        supervisor.wake()
        await _wait_status(coordinator, handle.run_id, RunStatus.COMPLETED)

        assert model.calls == 2
        events = (await store.read(handle.run_id, limit=200)).events
        event_types = [event.event_type for event in events]
        assert event_types.count("InterruptRequested") == 1
        assert event_types.count("RunCancelled") == 1
        assert event_types.count("RunResumed") == 1
        assert event_types.count("RunCreated") == 1
        assert (await coordinator.inspect(handle.run_id)).status is RunStatus.COMPLETED
    finally:
        model.release.set()
        await supervisor.stop(drain=False)
        await store.close()
