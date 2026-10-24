from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest
from cc_harness.run_model import RunStatus

from cc_harness.run_kernel import ActionRequest, ModelSegment, ReActKernel
from cc_harness.lease import LeaseManager
from cc_harness.run_model import ActionStatus, EffectClass, EvidenceKind, EvidenceRef, PlanNode
from cc_harness.run_events import EventActor
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
        # Recovery reconstructs state but must wait for an explicit continuation.
        assert (await coordinator.inspect(handle.run_id)).status is RunStatus.PAUSED
        await coordinator.resume(handle.run_id, "continue after crash")
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


class IdentityCompletionModel:
    async def complete(self, messages, tools):
        del messages, tools
        return ModelSegment(
            text="我是 cc-harness。",
            completion_candidate={
                "acceptance_criteria": ["answer"],
                "evidence": [
                    EvidenceRef(
                        "identity-answer",
                        EvidenceKind.TEST,
                        "sha256:identity-answer",
                        "runtime",
                        1.0,
                    ).to_dict()
                ],
            },
        )


class BadJsonThenAnswerModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return ModelSegment(
                text="",
                tool_calls=(
                    {"id": "bad-json-call", "name": "Read", "arguments": "{not json"},
                ),
            )
        if self.calls == 2:
            return ModelSegment(
                text="",
                tool_calls=(
                    {
                        "id": "good-read",
                        "name": "Read",
                        "arguments": '{"path": "README.md"}',
                    },
                ),
            )
        return await IdentityCompletionModel().complete((), ())


@pytest.mark.asyncio
async def test_worker_does_not_fail_run_on_malformed_tool_arguments(tmp_path) -> None:
    """Invalid provider JSON must become a recoverable observation."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(
            RunRequest("read README", ("answer",), interaction_mode="conversation")
        )
        model = BadJsonThenAnswerModel()
        worker = RunWorker(
            store,
            ReActKernel(model),
            worker_id="worker-bad-json",
            action_executor=_success,
        )
        for _ in range(4):
            view = await store.load_projection(handle.run_id)
            if view.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                break
            await worker.execute(await worker.claim(handle.run_id))
        events = (await store.read(handle.run_id)).events
        assert model.calls >= 2
        observations = [
            event for event in events if event.event_type == "ToolObservationCommitted"
        ]
        assert observations
        assert any(
            observation.payload.get("status") == "failed"
            for observation in observations
        )
        assert not any(event.event_type == "RunFailed" for event in events)
        # The provider error must be fed back to the model as a failed
        # observation.  Completion is still subject to the normal gate (the
        # test model does not mark its plan node done), but the run must not
        # die with an unhandled worker exception.
        assert (await store.load_projection(handle.run_id)).status is not RunStatus.FAILED_RECOVERABLE
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_worker_allows_final_segment_after_plan_marks_every_node_done(tmp_path) -> None:
    """A finished checklist must not become a scheduler dead end.

    A model can call task_plan, mark the last milestone done, and still need a
    second segment to write the final answer.  The old worker treated the
    absence of a dependency-ready node as RunBlocked and ended the run with
    ``no dependency-ready plan node`` even though the plan was complete.
    """

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("你是谁", ("answer",)))
        node = PlanNode(
            node_id="answer-question",
            kind="root",
            title="回答用户身份问题",
            acceptance_criteria=("answer",),
        )
        await coordinator.revise_plan(handle.run_id, (node,), reason="simple identity answer")
        await coordinator._append(
            handle.run_id,
            "TodoUpdated",
            {"todo": {
                "id": node.node_id,
                "title": node.title,
                "status": "done",
                "active_sessions": [handle.run_id],
                "plan_node_id": node.node_id,
            }},
            EventActor("coordinator", "test"),
        )
        worker = RunWorker(
            store,
            ReActKernel(IdentityCompletionModel()),
            worker_id="worker-plan-complete",
            action_executor=_success,
        )
        await worker.execute(await worker.claim(handle.run_id))
        events = (await store.read(handle.run_id)).events
        assert not any(
            event.event_type == "RunBlocked"
            and event.payload.get("reason") == "no dependency-ready plan node"
            for event in events
        )
        assert (await store.load_projection(handle.run_id)).status.value == "completed"
    finally:
        await store.close()


class _WatchdogStore:
    """Minimal store stub: the watchdog may read the status row only."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.projection_reads = 0

    async def load_run_record(self, run_id: str):
        del run_id
        return SimpleNamespace(status=self.status)

    async def load_projection(self, run_id: str):
        del run_id
        self.projection_reads += 1
        raise AssertionError("the cancellation watchdog must not rebuild the projection")


async def _watchdog_worker(tmp_path, status: str):
    """A real worker (for its constructor invariants) with a stubbed store."""
    project = tmp_path / "watchdog-project"
    project.mkdir()
    real = RunStore(project, data_root=tmp_path / "watchdog-data")
    await real.open()
    worker = RunWorker(real, ReActKernel(FakeModel()), worker_id="watchdog")
    fake = _WatchdogStore(status)
    worker.store = fake
    return real, worker, fake


@pytest.mark.asyncio
async def test_cancellation_watchdog_reads_status_row_not_projection(tmp_path) -> None:
    real, worker, store = await _watchdog_worker(tmp_path, "cancel_requested")
    execution = asyncio.create_task(asyncio.sleep(30))
    try:
        await asyncio.wait_for(worker._cancellation_watchdog("run-1", execution), timeout=2)

        # The watchdog requests cancellation; await the task so it settles.
        with contextlib.suppress(asyncio.CancelledError):
            await execution
        assert execution.cancelled()
        assert store.projection_reads == 0
    finally:
        execution.cancel()
        await real.close()


@pytest.mark.asyncio
async def test_cancellation_watchdog_leaves_running_task_alone(tmp_path) -> None:
    real, worker, store = await _watchdog_worker(tmp_path, "running")
    execution = asyncio.create_task(asyncio.sleep(30))
    try:
        watchdog = asyncio.create_task(worker._cancellation_watchdog("run-1", execution))
        await asyncio.sleep(0.3)
        assert not execution.cancelled()
        watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog
        assert store.projection_reads == 0
    finally:
        execution.cancel()
        await real.close()
