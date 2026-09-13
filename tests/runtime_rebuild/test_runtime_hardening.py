from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.memory.models import RecallResult
from cc_harness.memory.recall import layered_recall
from cc_harness.run_kernel import ActionRequest, SegmentContext
from cc_harness.run_events import EventActor
from cc_harness.run_model import ActionStatus, EffectClass
from cc_harness.run_projection import RunProjection
from cc_harness.run_store import RunStore
from cc_harness.worker import ModelInvocationTimeout, RunWorker


def test_action_idempotency_key_is_semantic_and_provider_overridable() -> None:
    first = ActionRequest(
        "model-call-a",
        "send_email",
        {"to": "user@example.com", "body": "hello"},
        EffectClass.EXTERNAL_SIDE_EFFECT,
    )
    second = ActionRequest(
        "model-call-b",
        "send_email",
        {"body": "hello", "to": "user@example.com"},
        EffectClass.EXTERNAL_SIDE_EFFECT,
    )
    assert first.normalized_args_digest == second.normalized_args_digest
    assert first.idempotency_key == second.idempotency_key

    explicit = ActionRequest(
        "model-call-c",
        "send_email",
        {"to": "user@example.com"},
        EffectClass.EXTERNAL_SIDE_EFFECT,
        idempotency_key="provider-request-42",
    )
    assert explicit.idempotency_key == "provider-request-42"
    # A key embedded by a provider protocol is not allowed to perturb the
    # semantic argument digest used for approvals and replay checks.
    embedded = ActionRequest(
        "model-call-d",
        "send_email",
        {"to": "user@example.com", "idempotency_key": "provider-request-42"},
        EffectClass.EXTERNAL_SIDE_EFFECT,
    )
    assert embedded.idempotency_key == explicit.idempotency_key
    assert embedded.normalized_args_digest == explicit.normalized_args_digest


def test_same_model_batch_is_deduplicated_before_execution(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worker = RunWorker(
        RunStore(project, data_root=tmp_path / "state"),
        object(),
        worker_id="worker-dedupe",
    )
    first = ActionRequest(
        "model-call-a",
        "Write",
        {"path": "README.md", "content": "same"},
        EffectClass.WORKSPACE_MUTATION,
    )
    duplicate = ActionRequest(
        "model-call-b",
        "Write",
        {"content": "same", "path": "README.md"},
        EffectClass.WORKSPACE_MUTATION,
    )
    selected, blocked = worker._deduplicate_action_requests(
        RunProjection.empty("run"), (first, duplicate)
    )
    assert selected == (first,)
    assert blocked is True


@pytest.mark.asyncio
async def test_progressive_memory_stops_at_first_matching_layer(tmp_path) -> None:
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("summary: stable coding preferences", encoding="utf-8")
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "session-v0001.md").write_text(
        "summary: database migration decisions\natom_ids:\n- atom-1\n",
        encoding="utf-8",
    )

    class Retriever:
        def __init__(self):
            self.calls: list[set[str] | None] = []
            self._store = SimpleNamespace(search_conversation=self.search_conversation)

        async def search_hybrid(self, query, top_k=5, *, layers=None):
            del query, top_k
            self.calls.append(layers)
            return [(SimpleNamespace(layer="L1", text="L1 fact", source="test"), 0.1)]

        async def search_conversation(self, query, *, limit=5, session_id=None):
            del query, limit, session_id
            return [{"role": "user", "content": "L0 turn"}]

    retriever = Retriever()
    result = await layered_recall(
        retriever,
        persona_path,
        scenarios_dir,
        "incident report",
        progressive=True,
    )
    assert isinstance(result, RecallResult)
    assert result.layers == ("L3", "L2", "L1")
    assert result.next_layer == "L0"
    assert result.atoms and result.atoms[0][0].text == "L1 fact"
    assert retriever.calls == [{"L1"}]

    persona_result = await layered_recall(
        retriever,
        persona_path,
        scenarios_dir,
        "coding preferences",
        progressive=True,
    )
    assert persona_result.layers == ("L3",)
    assert persona_result.next_layer == "L2"
    assert len(retriever.calls) == 1


@pytest.mark.asyncio
async def test_progressive_memory_falls_back_to_durable_l0(tmp_path) -> None:
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("summary: unrelated", encoding="utf-8")
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()

    class Retriever:
        _store = None

        def __init__(self):
            self._store = self

        async def search_hybrid(self, query, top_k=5, *, layers=None):
            del query, top_k, layers
            return []

        async def search_conversation(self, query, *, limit=5, session_id=None):
            assert query == "old incident"
            assert limit == 5
            assert session_id == "session-1"
            return [{"role": "assistant", "content": "old incident"}]

    result = await layered_recall(
        Retriever(),
        persona_path,
        scenarios_dir,
        "old incident",
        session_id="session-1",
        progressive=True,
    )
    assert result.layers == ("L3", "L2", "L1", "L0")
    assert result.conversation[0]["content"] == "old incident"
    assert result.next_layer is None


@pytest.mark.asyncio
async def test_progressive_memory_is_fail_soft_and_honors_requested_layers(tmp_path) -> None:
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("summary: unrelated", encoding="utf-8")
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()

    class BrokenRetriever:
        async def search_hybrid(self, query, top_k=5, *, layers=None):
            del query, top_k, layers
            raise RuntimeError("embedding service down")

    result = await layered_recall(
        BrokenRetriever(),
        persona_path,
        scenarios_dir,
        "query",
        layers=("L1",),
        progressive=True,
    )
    assert result.layers == ("L1",)
    assert result.atoms == []


@pytest.mark.asyncio
async def test_model_watchdog_bounds_a_hung_provider(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "state")

    class HangingKernel:
        async def execute_segment(self, context):
            del context
            await asyncio.Event().wait()

    worker = RunWorker(
        store, HangingKernel(), worker_id="worker-timeout", model_timeout_seconds=0.02
    )
    context = SegmentContext(run_id="run", projection=SimpleNamespace(), messages=())
    with pytest.raises(ModelInvocationTimeout):
        await worker._execute_model_segment(context)


@pytest.mark.asyncio
async def test_model_watchdog_does_not_wait_for_non_cooperative_provider(tmp_path) -> None:
    """A provider that swallows cancellation cannot wedge Worker cleanup."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "state")
    started = asyncio.Event()
    release = asyncio.Event()

    class NonCooperativeKernel:
        task: asyncio.Task | None = None

        async def execute_segment(self, context):
            del context
            self.task = asyncio.current_task()
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Simulate an HTTP/MCP adapter that delays cancellation while
                # it drains a transport.  The Worker must not await this path
                # indefinitely after its watchdog fires.
                await release.wait()
            return SimpleNamespace()

    kernel = NonCooperativeKernel()
    worker = RunWorker(
        store, kernel, worker_id="worker-non-cooperative", model_timeout_seconds=0.02
    )
    context = SegmentContext(run_id="run", projection=SimpleNamespace(), messages=())
    task = asyncio.create_task(worker._execute_model_segment(context))
    await asyncio.wait_for(started.wait(), timeout=1)
    started_at = time.monotonic()
    with pytest.raises(ModelInvocationTimeout):
        await asyncio.wait_for(task, timeout=0.6)
    assert time.monotonic() - started_at < 0.5

    # Release the deliberately detached provider task so the test leaves no
    # pending task behind for the next test/event loop teardown.
    release.set()
    assert kernel.task is not None
    await asyncio.wait_for(kernel.task, timeout=1)


@pytest.mark.asyncio
async def test_interrupt_watchdog_cancels_model_and_persists_cancelled(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "state").open()
    started = asyncio.Event()

    class HangingKernel:
        async def execute_segment(self, context):
            del context
            started.set()
            await asyncio.Event().wait()

    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("interrupt me", ("stop",)))
        worker = RunWorker(
            store,
            HangingKernel(),
            worker_id="worker-watchdog",
            model_timeout_seconds=60,
        )
        task = asyncio.create_task(worker.execute(await worker.claim(handle.run_id)))
        await asyncio.wait_for(started.wait(), timeout=1)
        receipt = await coordinator.interrupt(handle.run_id, "user pressed Ctrl+C")
        assert receipt.status.value == "cancel_requested"
        await asyncio.wait_for(task, timeout=1)
        view = await coordinator.inspect(handle.run_id)
        assert view.status.value == "cancelled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_resolves_latest_unknown_attempt(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "state").open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("reconcile", ("done",)))
        worker = RunWorker(store, object(), worker_id="worker-reconcile")
        lease = await worker.claim(handle.run_id)
        request = ActionRequest(
            "send-1",
            "send_email",
            {"to": "user@example.com"},
            EffectClass.EXTERNAL_SIDE_EFFECT,
            idempotency_key="mail-1",
        )
        contract = worker.contracts.get(request.tool_name)
        await worker._plan_action(lease, request, contract.effect_class, contract.digest, attempt=1)
        await worker._append(lease, "ActionPrepared", {"action_id": request.action_id, "attempt": 1})
        await worker._append(lease, "ActionStarted", {"action_id": request.action_id, "attempt": 1})
        await worker._append(
            lease,
            "ActionOutcomeUnknown",
            {"action_id": request.action_id, "attempt": 1, "reason": "lost receipt"},
        )
        # Same logical action, a second attempt also becomes uncertain.  The
        # coordinator must resolve attempt 2, never the older attempt 1.
        await worker._plan_action(lease, request, contract.effect_class, contract.digest, attempt=2)
        await worker._append(lease, "ActionPrepared", {"action_id": request.action_id, "attempt": 2})
        await worker._append(lease, "ActionStarted", {"action_id": request.action_id, "attempt": 2})
        await worker._append(
            lease,
            "ActionOutcomeUnknown",
            {"action_id": request.action_id, "attempt": 2, "reason": "lost receipt"},
        )
        await worker._append(lease, "RunBlocked", {"reason": "reconcile required"})
        # A process can be cancelled after the uncertain action has already
        # released its worker lease.  Reconciliation remains valid at this
        # terminal recovery boundary; continuation can be requested later.
        await store.release_lease(handle.run_id, lease.epoch)
        cancelled = await coordinator.cancel(handle.run_id, "cancel after crash")
        assert cancelled.status.value == "cancelled"
        resolved = await coordinator.reconcile_action(
            handle.run_id,
            request.action_id,
            ActionStatus.SUCCEEDED,
            reason="provider receipt found",
        )
        attempts = {item.attempt: item.status for item in resolved.projection.actions}
        assert attempts[1] is ActionStatus.OUTCOME_UNKNOWN
        assert attempts[2] is ActionStatus.SUCCEEDED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_store_artifact_gc_keeps_event_references(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "state").open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(RunRequest("gc", ("done",)))
        protected = store.artifacts.put_text("event root")
        orphan = store.artifacts.put_text("orphan")
        old = time.time() - 3600
        os.utime(store.artifacts.object_path(protected.digest), (old, old))
        os.utime(store.artifacts.object_path(orphan.digest), (old, old))
        await coordinator._append(
            handle.run_id,
            "MemoryCheckpointCommitted",
            {
                "checkpoint_id": "checkpoint-1",
                "source_digest": protected.digest,
                "captured_count": 1,
            },
            actor=EventActor("runtime", "test"),
            artifact_refs=(protected.digest,),
        )
        roots = await store.referenced_artifact_digests()
        assert protected.digest in roots
        report = await store.collect_artifact_garbage(grace_period_seconds=0)
        assert protected.digest in report.protected
        assert orphan.digest in report.deleted
    finally:
        await store.close()
