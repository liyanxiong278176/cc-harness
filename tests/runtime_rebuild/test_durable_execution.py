from __future__ import annotations

import asyncio

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.durable_runtime import DurableModelAdapter
from cc_harness.llm import PendingToolCall, StreamEvent
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus, EvidenceKind, EvidenceRef, PlanNode
from cc_harness.run_store import RunStore
from cc_harness.supervisor import LocalSupervisor
from cc_harness.worker import ActionExecutionResult, RunWorker


EVIDENCE = EvidenceRef(
    "evidence-durable",
    EvidenceKind.TEST,
    "sha256:durable-test",
    "pytest",
    1.0,
)


class TwoStepModel:
    def __init__(self, tool_name: str, arguments: dict) -> None:
        self.calls = 0
        self.tool_name = tool_name
        self.arguments = arguments

    async def complete(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return ModelSegment(
                text="acting",
                tool_calls=(
                    {
                        "id": f"action-{self.calls}",
                        "name": self.tool_name,
                        "arguments": self.arguments,
                    },
                ),
            )
        return ModelSegment(
            text="verified",
            completion_candidate={
                "acceptance_criteria": ["done"],
                "evidence": [EVIDENCE.to_dict()],
            },
        )


async def _success(_request):
    return ActionExecutionResult(ActionStatus.SUCCEEDED)


class FakeStreamingLLM:
    def __init__(self) -> None:
        self.model = "fake"

    async def chat(self, messages, tools):
        del messages, tools
        yield StreamEvent(
            kind="done",
            content=(
                "verified <cc-harness-complete>"
                '{"acceptance_criteria":["done"],"evidence":[]}'
                "</cc-harness-complete>"
            ),
            reasoning_content="reasoning trace",
            pending=[
                PendingToolCall(
                    id="call-1",
                    name="Read",
                    arguments_json='{"path":"README.md"}',
                )
            ],
            finish_reason="tool_calls",
        )


class MalformedCompletionLLM:
    def __init__(self) -> None:
        self.model = "fake"

    async def chat(self, messages, tools):
        del messages, tools
        yield StreamEvent(
            kind="done",
            content=(
                "kept for repair <cc-harness-complete>"
                '{"acceptance_criteria":["done"],"evidence":["not-an-evidence-ref"]}'
                "</cc-harness-complete>"
            ),
        )


class ExplodingModel:
    async def complete(self, messages, tools):
        del messages, tools
        raise OSError("simulated artifact publication failure")


async def _wait_for_terminal(supervisor, store, run_id: str):
    for _ in range(80):
        await asyncio.sleep(0.01)
        await supervisor.tick()
        status = (await store.load_projection(run_id)).status.value
        if status in {"completed", "blocked", "stalled", "failed_terminal"}:
            return status
    return (await store.load_projection(run_id)).status.value


async def _wait_for_status(supervisor, store, run_id: str, expected: str):
    for _ in range(80):
        await asyncio.sleep(0.01)
        await supervisor.tick()
        view = await store.load_projection(run_id)
        if view.status.value == expected:
            return view
    return await store.load_projection(run_id)


@pytest.mark.asyncio
async def test_durable_model_adapter_preserves_tool_calls_and_completion_marker() -> None:
    segment = await DurableModelAdapter(FakeStreamingLLM()).complete(({"role": "user"},), ())
    assert segment.tool_calls[0]["name"] == "Read"
    assert segment.tool_calls[0]["arguments"] == {"path": "README.md"}
    assert segment.completion_candidate == {"acceptance_criteria": ["done"], "evidence": []}
    assert segment.reasoning_content == "reasoning trace"


@pytest.mark.asyncio
async def test_durable_model_adapter_keeps_malformed_completion_for_repair() -> None:
    segment = await DurableModelAdapter(MalformedCompletionLLM()).complete(({"role": "user"},), ())
    assert segment.completion_candidate is None
    assert "not-an-evidence-ref" in segment.text


@pytest.mark.asyncio
async def test_supervisor_requeues_and_completes_multiple_durable_segments(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("durable task", ("done",)))
        model = TwoStepModel("Read", {"path": "README.md"})

        def factory(_run_id):
            return RunWorker(
                store,
                ReActKernel(model),
                worker_id="durable-worker",
                action_executor=_success,
            )

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        status = await _wait_for_terminal(supervisor, store, handle.run_id)
        assert status == "completed"
        event_types = [event.event_type for event in (await store.read(handle.run_id)).events]
        # The tool call and verification call now belong to one recoverable
        # Segment; yielding is reserved for a segment boundary, not each
        # model/tool round.
        assert event_types.count("RunSegmentStarted") == 1
        assert "ToolObservationCommitted" in event_types
        assert model.calls == 2
        await supervisor.stop()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unhandled_worker_exception_is_persisted_as_recoverable_failure(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("durable task", ("done",)))

        def factory(_run_id):
            return RunWorker(
                store,
                ReActKernel(ExplodingModel()),
                worker_id="failing-worker",
                action_executor=_success,
            )

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        try:
            view = await _wait_for_status(
                supervisor,
                store,
                handle.run_id,
                "failed_recoverable",
            )
            assert view.status.value == "failed_recoverable"
            events = (await store.read(handle.run_id)).events
            failure = next(event for event in events if event.event_type == "RunFailed")
            assert failure.payload["reason"].startswith(
                "unhandled worker exception: OSError: simulated artifact publication failure | "
            )
            assert failure.payload["target_status"] == "failed_recoverable"
        finally:
            await supervisor.stop()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_plan_nodes_finish_serially_before_run_completion(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("plan task", ("done",)))
        await coordinator.create_plan(
            handle.run_id,
            (
                PlanNode("first", "step", owned_paths=("src",)),
                PlanNode("second", "step", depends_on=("first",), owned_paths=("tests",)),
            ),
        )
        model = TwoStepModel("Read", {"path": "README.md"})

        def factory(_run_id):
            return RunWorker(
                store,
                ReActKernel(model),
                worker_id="plan-worker",
                action_executor=_success,
            )

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        status = await _wait_for_terminal(supervisor, store, handle.run_id)
        assert status == "completed"
        events = (await store.read(handle.run_id)).events
        event_types = [event.event_type for event in events]
        assert event_types.count("PlanNodeStarted") == 2
        assert event_types.count("PlanNodeCompleted") == 2
        assert event_types.index("CompletionAccepted") > event_types.index("PlanNodeCompleted")
        await supervisor.stop()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_granted_approval_resumes_persisted_action_without_replanning(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("approved task", ("done",)))
        model = TwoStepModel("run_command", {"command": "echo approved"})

        def factory(_run_id):
            return RunWorker(
                store,
                ReActKernel(model),
                worker_id="approval-worker",
                action_executor=_success,
            )

        supervisor = LocalSupervisor(store, factory, max_workers=1, poll_interval=0.01)
        try:
            await supervisor.tick()
            view = await _wait_for_status(supervisor, store, handle.run_id, "awaiting_approval")
            assert view.status.value == "awaiting_approval"
            approval = view.approvals[0]
            await coordinator.approve(
                run_id=handle.run_id,
                approval_id=approval.approval_id,
                action_args_digest=approval.action_args_digest,
            )
            status = await _wait_for_terminal(supervisor, store, handle.run_id)
            assert status == "completed"
            event_types = [event.event_type for event in (await store.read(handle.run_id)).events]
            assert event_types.count("ActionPlanned") == 1
            assert event_types.count("ActionSucceeded") == 1
        finally:
            await supervisor.stop()
    finally:
        await store.close()
