from __future__ import annotations

import asyncio
import time

import pytest

from cc_harness.run_kernel import ActionRequest, ModelSegment, ReActKernel
from cc_harness.lease import LeaseManager
from cc_harness.run_model import ActionStatus, EffectClass, EvidenceKind, EvidenceRef
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
        event_types = [event.event_type for event in events]
        positions = {
            event_type: event_types.index(event_type)
            for event_type in (
                "AssistantMessageCommitted",
                "ActionPrepared",
                "ActionStarted",
                "ToolObservationCommitted",
                "ActionSucceeded",
                "RunSegmentFinished",
                "RunYielded",
            )
        }
        assert positions["AssistantMessageCommitted"] < positions["ToolObservationCommitted"]
        assert positions["ActionPrepared"] < positions["ActionStarted"]
        assert positions["ActionStarted"] < positions["ToolObservationCommitted"]
        assert positions["ToolObservationCommitted"] < positions["ActionSucceeded"]
        assert positions["ActionSucceeded"] < positions["RunSegmentFinished"]
        assert positions["RunSegmentFinished"] < positions["RunYielded"]
        projection = await store.load_projection(handle.run_id)
        assert projection.actions[0].status is ActionStatus.SUCCEEDED
        assert await store.current_lease(handle.run_id) is None
    finally:
        await store.close()


async def _success(request):
    return ActionExecutionResult(ActionStatus.SUCCEEDED, read_paths=("README.md",))


class RecoveryCompletionModel:
    async def complete(self, messages, tools):
        del messages, tools
        return ModelSegment(
            text="recovered",
            completion_candidate={
                "acceptance_criteria": ["read"],
                "evidence": [
                    EvidenceRef(
                        "recovery-evidence",
                        EvidenceKind.TEST,
                        "sha256:recovery-evidence",
                        "pytest",
                        1.0,
                    ).to_dict()
                ],
            },
        )


@pytest.mark.asyncio
async def test_worker_retries_only_idempotent_read_after_lease_expiry(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("worker task", ("read",)))
        first_worker = RunWorker(
            store,
            ReActKernel(RecoveryCompletionModel()),
            worker_id="worker-crashed",
            lease_manager=LeaseManager(store, ttl_seconds=1.0),
            action_executor=_success,
        )
        lease = await first_worker.claim(handle.run_id)
        request = ActionRequest(
            "crashed-read",
            "Read",
            {"path": "README.md"},
            effect_class=EffectClass.READ_ONLY,
        )
        contract = first_worker.contracts.get("Read")
        await first_worker._plan_action(lease, request, EffectClass.READ_ONLY, contract.digest)
        await first_worker._append(
            lease,
            "ActionPrepared",
            {"action_id": request.action_id, "attempt": 1},
        )
        await first_worker._append(
            lease,
            "ActionStarted",
            {"action_id": request.action_id, "attempt": 1},
        )
        await asyncio.sleep(max(0.0, lease.expires_at - time.time() + 0.05))
        await first_worker.lease_manager.reclaim_expired(handle.run_id)

        calls = 0

        async def counted_success(action):
            nonlocal calls
            calls += 1
            return await _success(action)

        second_worker = RunWorker(
            store,
            ReActKernel(RecoveryCompletionModel()),
            worker_id="worker-recovered",
            action_executor=counted_success,
        )
        await second_worker.execute(await second_worker.claim(handle.run_id))

        assert calls == 1
        events = (await store.read(handle.run_id)).events
        assert any(
            event.event_type == "ActionCancelled"
            and event.payload.get("attempt") == 1
            for event in events
        )
        assert any(
            event.event_type == "ActionSucceeded"
            and event.payload.get("attempt") == 2
            for event in events
        )
        assert (await store.load_projection(handle.run_id)).status.value == "completed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_worker_propagates_action_cancellation_to_supervisor(tmp_path) -> None:
    """Supervisor cancellation must not be converted into an unknown result."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    started = asyncio.Event()

    async def blocking_executor(request):
        del request
        started.set()
        await asyncio.Event().wait()

    try:
        handle = await RunCoordinator(store).submit(RunRequest("cancel action", ("read",)))
        worker = RunWorker(
            store,
            ReActKernel(FakeModel()),
            worker_id="worker-cancelled",
            action_executor=blocking_executor,
        )
        lease = await worker.claim(handle.run_id)
        request = ActionRequest(
            "cancelled-read",
            "Read",
            {"path": "README.md"},
            effect_class=EffectClass.READ_ONLY,
        )
        task = asyncio.create_task(worker._execute_action(lease, request))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await store.close()
