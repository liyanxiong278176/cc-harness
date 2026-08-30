from __future__ import annotations

from types import SimpleNamespace

import pytest

from cc_harness.capability_runtime import ContextBuild
from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.context import CompactionStats, CompactionTier
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness import interaction_history
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus, EvidenceKind, EvidenceRef
from cc_harness.run_store import RunRecordView, RunStore
from cc_harness.worker import ActionExecutionResult, RunWorker


EVIDENCE = EvidenceRef(
    "loop-evidence",
    EvidenceKind.TEST,
    "sha256:loop-evidence",
    "pytest",
    1.0,
)


class TwoRoundModel:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[tuple[dict, ...]] = []

    async def complete(self, messages, tools):
        del tools
        self.calls += 1
        self.messages.append(tuple(dict(message) for message in messages))
        if self.calls == 1:
            return ModelSegment(
                text="inspect",
                tool_calls=({"id": "read-1", "name": "Read", "arguments": {"path": "a.txt"}},),
            )
        return ModelSegment(
            text="verified",
            completion_candidate={
                "acceptance_criteria": ["done"],
                "evidence": [EVIDENCE.to_dict()],
            },
        )


async def _success(request):
    return ActionExecutionResult(ActionStatus.SUCCEEDED, read_paths=(request.arguments["path"],))


@pytest.mark.asyncio
async def test_model_sees_only_committed_observation_in_same_segment(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("inspect", ("done",)))
        model = TwoRoundModel()
        worker = RunWorker(
            store,
            ReActKernel(model),
            worker_id="loop-worker",
            action_executor=_success,
        )
        await worker.execute(await worker.claim(handle.run_id))

        assert model.calls == 2
        second = model.messages[1]
        assert any(message.get("role") == "assistant" for message in second)
        observations = [message for message in second if message.get("role") == "tool"]
        assert len(observations) == 1
        assert observations[0]["tool_call_id"] == "read-1"
        assert observations[0]["_cc_harness_untrusted"] is True
        events = (await store.read(handle.run_id)).events
        event_types = [event.event_type for event in events]
        assert event_types.count("RunSegmentStarted") == 1
        assert event_types.index("AssistantMessageCommitted") < event_types.index(
            "ToolObservationCommitted"
        )
        assert event_types.index("ToolObservationCommitted") < event_types.index("ActionSucceeded")
    finally:
        await store.close()


class FailClosedCapabilityRuntime:
    context_config = SimpleNamespace(enabled=True, context_window=1_000_000, fail_closed=True)

    async def build_context(self, _projection, base_messages, _tool_specs, *, query=""):
        del query
        stats = CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=100,
            after_tokens=100,
            ratio_before=0.99,
            ratio_after=0.99,
            error="summary provider unavailable",
        )
        return ContextBuild(
            messages=tuple(dict(message) for message in base_messages),
            source_message_count=len(base_messages),
            projected_message_count=len(base_messages),
            source_digest="sha256:source",
            projection_digest="sha256:projection",
            compaction=stats,
        )


@pytest.mark.asyncio
async def test_fail_closed_context_blocks_before_model_invocation(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("inspect", ("done",)))
        model = TwoRoundModel()
        worker = RunWorker(
            store,
            ReActKernel(model),
            worker_id="fail-closed-worker",
            capability_runtime=FailClosedCapabilityRuntime(),
        )
        await worker.execute(await worker.claim(handle.run_id))

        assert model.calls == 0
        events = (await store.read(handle.run_id)).events
        event_types = [event.event_type for event in events]
        assert "ContextCompacted" in event_types
        assert "RunBlocked" in event_types
        assert "ModelInvocationStarted" not in event_types
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recall_run_context_uses_durable_lineage_record_without_projection_attributes() -> None:
    class Store:
        async def load_projection(self, _run_id):
            return SimpleNamespace(children=())

        async def load_run_record(self, _run_id):
            return RunRecordView("run-1", "running", 0, "digest", "parent-1", "pred-1")

        async def read(self, _run_id, *, after=0, limit=1000):
            del after, limit
            return SimpleNamespace(events=(), next_cursor=None)

    client = object.__new__(DurableRuntimeClient)
    client.store = Store()

    result = await client._recall_run_context("run-1", {"source_run_id": "parent-1"})

    assert result.is_error is False
    assert "events" in result.llm_text


@pytest.mark.asyncio
async def test_resume_reason_is_materialized_as_latest_user_turn(monkeypatch) -> None:
    """A natural-language resume must reach the next model invocation."""

    async def _events(_store, _run_id):
        return (
            SimpleNamespace(
                event_type="RunResumed",
                payload={"reason": "继续验证前端并提交 CompletionCandidate"},
            ),
        )

    monkeypatch.setattr(interaction_history, "_read_events", _events)
    projection = SimpleNamespace(run_id="run-1")
    messages = await interaction_history.materialize_interaction_messages(
        SimpleNamespace(), projection
    )

    assert messages == (
        {
            "role": "user",
            "content": "继续验证前端并提交 CompletionCandidate",
            "_cc_harness_resume_reason": True,
            "_context_mandatory": True,
        },
    )
