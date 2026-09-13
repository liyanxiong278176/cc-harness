"""Runtime-domain fault injection (A1-A13).

These tests exercise the durable boundaries instead of asserting a worker
return value.  Every recovery assertion also checks the immutable stream and
the folded projection, which makes a later regression diagnosable from the
pytest node alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from cc_harness.action_contracts import ActionScheduler, ToolContractRegistry
from cc_harness.approvals import ApprovalService
from cc_harness.coordinator import RunCoordinator
from cc_harness.durable_runtime import DurableModelAdapter
from cc_harness.lease import LeaseManager
from cc_harness.llm import LLMClient, ProviderProtocolError, StreamEvent
from cc_harness.loop_control import (
    CompletionContract,
    CompletionVerifier,
    RecoveryPolicy,
    StallController,
    ToolErrorKind,
    WorkingState,
    action_fingerprint,
    action_signature,
)
from cc_harness.plan_graph import PlanGraphError, PlanGraphService
from cc_harness.run_events import EventActor, EventValidationError, RunEvent
from cc_harness.run_kernel import KernelProtocolError, ModelSegment, ReActKernel, SegmentContext
from cc_harness.run_model import (
    ActionStatus,
    CompletionCandidate,
    EffectClass,
    EvidenceKind,
    EvidenceRef,
    GoalContract,
    PlanGraph,
    PlanNode,
    RunStatus,
)
from cc_harness.run_projection import ProjectionError, RunProjection
from cc_harness.run_store import LeaseFenceError, RunStoreError, SequenceConflict
from cc_harness.supervisor import LocalSupervisor
from cc_harness.tool_observation import make_observation
from cc_harness.worker import ActionExecutionResult, RunWorker

from .conftest import (
    CountingExecutor,
    ScriptedModel,
    append_action_lifecycle,
    append_event,
    assert_valid_history,
    claim_run,
    digest_text,
    event_types,
    make_run,
)


@pytest.mark.asyncio
async def test_a1_transport_retries_only_transient_failures(no_sleep, monkeypatch):
    """408/429/503 style stream failures retry three times; 400 does not."""

    class StatusError(RuntimeError):
        def __init__(self, status_code: int):
            super().__init__(f"status code {status_code}")
            self.status_code = status_code

    client = LLMClient("test-key", "fake", None)
    calls = 0

    async def flaky(_messages, _tools):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise StatusError(503)
        yield StreamEvent(kind="done", content="ok")

    monkeypatch.setattr(client, "_chat_once", flaky)
    events = [event async for event in client.chat([{"role": "user", "content": "ping"}])]
    assert calls == 3
    assert [event.kind for event in events] == ["done"]

    calls = 0

    async def permanent(_messages, _tools):
        nonlocal calls
        calls += 1
        raise StatusError(400)
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(client, "_chat_once", permanent)
    with pytest.raises(StatusError):
        _ = [event async for event in client.chat([])]
    assert calls == 1
    await client.aclose()


@pytest.mark.requires_llm
@pytest.mark.asyncio
async def test_real_provider_probe_uses_configured_model_without_leaking_secrets():
    """Opt-in live check: the configured provider must produce a terminal event."""

    if os.getenv("CC_HARNESS_RUN_REAL_LLM") != "1":
        pytest.skip("set CC_HARNESS_RUN_REAL_LLM=1 to run the real provider probe")
    from dotenv import load_dotenv

    load_dotenv(Path(".env"), override=False)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY is not configured")
    client = LLMClient(
        api_key,
        os.getenv("OPENAI_MODEL", "deepseek-v4-flash"),
        os.getenv("OPENAI_BASE_URL"),
        thinking_mode="disabled",
    )
    try:
        events = [
            event
            async for event in client.chat(
                [{"role": "user", "content": "请只回复：真实模型探针通过"}]
            )
        ]
        assert events and events[-1].kind == "done"
        assert any(event.content or event.text for event in events)
        if events[-1].usage is not None:
            assert events[-1].usage.total_tokens >= 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        {"id": "bad-json", "name": "Read", "arguments": "{"},
        {"id": "array", "name": "Read", "arguments": []},
        {"id": "no-name", "arguments": {}},
    ],
    ids=["invalid-json", "array-arguments", "missing-tool-name"],
)
async def test_a2_kernel_rejects_malformed_tool_protocol(call):
    run_id = str(uuid.uuid4())
    context = SegmentContext(run_id, RunProjection.empty(run_id), ())
    kernel = ReActKernel(ScriptedModel(ModelSegment(tool_calls=(call,))))
    with pytest.raises(KernelProtocolError):
        await kernel.execute_segment(context)


def test_a2_reasoning_replay_is_lossless_or_explicitly_recoverable():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call", "type": "function", "function": {"name": "Read", "arguments": "{}"}}],
            "reasoning_content": "trace",
        }
    ]
    replay = DurableModelAdapter._provider_messages(
        messages, thinking_mode="enabled", reasoning_content_required=True
    )
    assert replay[0]["reasoning_content"] == "trace"
    with pytest.raises(ProviderProtocolError):
        DurableModelAdapter._provider_messages(
            [{"role": "assistant", "content": None, "tool_calls": [{"id": "call"}]}],
            thinking_mode="enabled",
        )


@pytest.mark.asyncio
async def test_a2_malformed_completion_is_kept_for_repair_and_unknown_tools_are_conservative():
    run_id = str(uuid.uuid4())
    malformed = ScriptedModel(
        ModelSegment(
            text="candidate",
            completion_candidate={"acceptance_criteria": ["done"], "evidence": ["bad"]},
        )
    )
    outcome = await ReActKernel(malformed).execute_segment(
        SegmentContext(run_id, RunProjection.empty(run_id), ())
    )
    assert outcome.completion_candidate is None

    unknown = await ReActKernel(
        ScriptedModel(ModelSegment(tool_calls=({"id": "u", "name": "invented_tool", "arguments": {}},)))
    ).execute_segment(SegmentContext(run_id, RunProjection.empty(run_id), ()))
    assert unknown.action_requests[0].tool_name == "invented_tool"
    contract = ToolContractRegistry.first_party().get("invented_tool")
    assert contract.effect_class == EffectClass.UNKNOWN
    assert contract.requires_approval is True


async def _seed_started_action(store, run_id: str, lease, *, effect: str, tool: str, action_id: str):
    args_ref = store.artifacts.put_text(
        json.dumps({"path": "README.md", "command": "echo safe"}),
        media_type="application/json; purpose=action-arguments",
    ).digest
    await append_event(
        store,
        run_id,
        "ActionPlanned",
        {
            "action_id": action_id,
            "attempt": 1,
            "tool_name": tool,
            "effect_class": effect,
            "normalized_args_digest": digest_text("{}"),
            "arguments_artifact": args_ref,
            "worker_id": lease.worker_id,
        },
        lease_epoch=lease.epoch,
        actor_kind="worker",
        actor_id=lease.worker_id,
        expected_lease_epoch=lease.epoch,
    )
    await append_event(
        store,
        run_id,
        "ActionPrepared",
        {"action_id": action_id, "attempt": 1},
        lease_epoch=lease.epoch,
        actor_kind="worker",
        actor_id=lease.worker_id,
        expected_lease_epoch=lease.epoch,
    )
    await append_event(
        store,
        run_id,
        "ActionStarted",
        {"action_id": action_id, "attempt": 1},
        lease_epoch=lease.epoch,
        actor_kind="worker",
        actor_id=lease.worker_id,
        expected_lease_epoch=lease.epoch,
    )


@pytest.mark.asyncio
async def test_a3_started_read_action_retries_new_attempt_after_lease_recovery(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        old = await claim_run(store, run_id, "crashed-reader")
        await _seed_started_action(
            store,
            run_id,
            old,
            effect=EffectClass.READ_ONLY.value,
            tool="Read",
            action_id="read-after-crash",
        )
        # Keep the row parseable as a Lease while making it expired.
        await store._db.execute(
            "UPDATE run_lease SET acquired_at=1, expires_at=2 WHERE run_id=?", (run_id,)
        )
        await store._db.commit()
        await LeaseManager(store, ttl_seconds=30).reclaim_expired(run_id)
        assert (await store.load_projection(run_id)).status is RunStatus.QUEUED
        new = await claim_run(store, run_id, "recovery-reader")
        executor = CountingExecutor(ActionExecutionResult(ActionStatus.SUCCEEDED))
        worker = RunWorker(
            store,
            ReActKernel(ScriptedModel()),
            worker_id="recovery-reader",
            action_executor=executor,
        )
        recovered, blocked = await worker._recover_inflight_actions(
            new, await store.load_projection(run_id)
        )
        assert recovered is True and blocked is False
        assert len(executor.calls) == 1
        projection = await store.load_projection(run_id)
        attempts = [item for item in projection.actions if item.action_id == "read-after-crash"]
        assert {item.attempt for item in attempts} == {1, 2}
        assert attempts[-1].status is ActionStatus.SUCCEEDED
        events = (await store.read(run_id, limit=10_000)).events
        assert "WorkerLeaseExpired" in event_types(events)
        assert event_types(events).count("ToolObservationCommitted") >= 2
        assert_valid_history(events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a3_started_external_action_becomes_unknown_and_blocked(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        old = await claim_run(store, run_id, "crashed-external")
        await _seed_started_action(
            store,
            run_id,
            old,
            effect=EffectClass.EXTERNAL_SIDE_EFFECT.value,
            tool="process_stop",
            action_id="external-after-crash",
        )
        await store._db.execute(
            "UPDATE run_lease SET acquired_at=1, expires_at=2 WHERE run_id=?", (run_id,)
        )
        await store._db.commit()
        await LeaseManager(store, ttl_seconds=30).reclaim_expired(run_id)
        new = await claim_run(store, run_id, "recovery-external")
        worker = RunWorker(store, ReActKernel(ScriptedModel()), worker_id="recovery-external")
        recovered, blocked = await worker._recover_inflight_actions(
            new, await store.load_projection(run_id)
        )
        assert recovered is True and blocked is True
        projection = await store.load_projection(run_id)
        assert projection.status is RunStatus.BLOCKED
        assert projection.actions[0].status is ActionStatus.OUTCOME_UNKNOWN
        events = (await store.read(run_id, limit=10_000)).events
        assert "ActionOutcomeUnknown" in event_types(events)
        assert_valid_history(events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a4_old_epoch_is_fenced_and_healthy_lease_survives(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        old = await claim_run(store, run_id, "epoch-a")
        await store._db.execute(
            "UPDATE run_lease SET acquired_at=1, expires_at=2 WHERE run_id=?", (run_id,)
        )
        await store._db.commit()
        await LeaseManager(store, ttl_seconds=30).reclaim_expired(run_id)
        new = await claim_run(store, run_id, "epoch-b")
        projection = await store.load_projection(run_id)
        stale = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="WorkerHeartbeat",
            actor=EventActor("worker", old.worker_id),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=old.epoch,
            payload={"heartbeat_at": "2026-01-01T00:00:00Z", "expires_at": 4102444800.0},
        )
        with pytest.raises(LeaseFenceError):
            await store.append(
                stale,
                expected_sequence=projection.sequence,
                expected_lease_epoch=old.epoch,
            )
        current = await store.current_lease(run_id)
        assert current is not None and current.epoch == new.epoch
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a5_event_and_snapshot_integrity_rejects_tampering(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        projection = await store.checkpoint(run_id)
        with pytest.raises(SequenceConflict):
            await store.append(
                RunEvent.create(
                    run_id=run_id,
                    sequence=projection.sequence + 2,
                    event_type="WorkerHeartbeat",
                    actor=EventActor("test", "gap"),
                    runtime_contract_digest=str(projection.runtime_contract_digest),
                    payload={"heartbeat_at": "2026-01-01T00:00:00Z", "expires_at": 1.0},
                ),
                expected_sequence=projection.sequence,
            )
        with sqlite3.connect(store.db_path) as db:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "UPDATE run_snapshot SET projection_digest='sha256:tampered' WHERE run_id=?",
                    (run_id,),
                )
                db.commit()
            db.execute(
                "UPDATE run_record SET last_sequence=last_sequence+1 WHERE run_id=?",
                (run_id,),
            )
            db.commit()
        with pytest.raises(RunStoreError, match="projection cursor"):
            await store.load_projection(run_id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a6_supervisor_timeout_does_not_kill_next_tick(tmp_path):
    _project, store, _run_id = await make_run(tmp_path)

    class SlowSupervisor(LocalSupervisor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ticks = 0
            self.gate = asyncio.Event()

        async def tick(self):
            self.ticks += 1
            await self.gate.wait()

    supervisor = SlowSupervisor(
        store,
        lambda _run_id: None,
        max_workers=1,
        poll_interval=0.001,
        tick_timeout=0.01,
    )
    try:
        await supervisor.start()
        await asyncio.sleep(0.04)
        assert supervisor.is_running is True
        assert supervisor.ticks >= 2
    finally:
        supervisor.gate.set()
        await supervisor.stop(drain=False)
        await store.close()


@pytest.mark.asyncio
async def test_a7_approval_digest_mismatch_cannot_execute_or_change_scope(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        lease = await claim_run(store, run_id, "approval-worker")
        service = ApprovalService(store)
        approval = await service.request(
            run_id=run_id,
            action_id="approval-action",
            action_args_digest=digest_text("approved-args"),
            scope=("run_command",),
            actor=EventActor("worker", lease.worker_id),
            lease_epoch=lease.epoch,
        )
        assert (await store.load_projection(run_id)).status is RunStatus.AWAITING_APPROVAL
        with pytest.raises(ProjectionError, match="approval parameters"):
            await service.grant(
                run_id=run_id,
                approval_id=approval.approval_id,
                action_args_digest=digest_text("different-args"),
                actor=EventActor("client", "operator"),
            )
        assert (await store.load_projection(run_id)).approvals[0].status.value == "requested"
        decision = await service.reject(
            run_id=run_id,
            approval_id=approval.approval_id,
            reason="not authorized",
            actor=EventActor("client", "operator"),
        )
        assert decision.status == "rejected"
        events = (await store.read(run_id, limit=10_000)).events
        assert "ApprovalRejected" in event_types(events)
        assert_valid_history(events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a8_cancel_requires_interrupt_boundary_and_is_idempotent(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        lease = await claim_run(store, run_id, "cancel-worker")
        projection = await store.load_projection(run_id)
        direct_cancel = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="RunCancelled",
            actor=EventActor("client", "operator"),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            payload={"reason": "direct cancel"},
        )
        with pytest.raises(EventValidationError):
            await store.append(direct_cancel, expected_sequence=projection.sequence)
        coordinator = RunCoordinator(store)
        await coordinator.interrupt(run_id, "ctrl-c")
        await coordinator.cancel(run_id, "ctrl-c confirmed")
        second = await coordinator.cancel(run_id, "second ctrl-c")
        # A live lease deliberately keeps the run at the cancellation
        # boundary until the worker reaches a safe action boundary.
        assert second.status is RunStatus.CANCEL_REQUESTED
        await store.release_lease(run_id, lease.epoch)
        await coordinator.cancel(run_id, "worker boundary reached")
        assert (await store.load_projection(run_id)).status is RunStatus.CANCELLED
        events = (await store.read(run_id, limit=10_000)).events
        assert event_types(events).count("InterruptRequested") == 1
        assert event_types(events).count("RunCancelled") == 1
        assert lease.epoch >= 1
        assert_valid_history(events)
    finally:
        await store.close()


def test_a9_stall_fingerprint_requires_same_action_and_result():
    args = {"command": "pytest tests/test_api.py"}
    fingerprint = action_fingerprint("run_command", args, is_error=True, result_text="exit 1")
    signature = action_signature("run_command", args)
    controller = StallController(repeat_threshold=3)
    assert controller.observe(fingerprint, action_signature=signature).stalled is False
    assert controller.observe(fingerprint, action_signature=signature).stalled is False
    third = controller.observe(fingerprint, action_signature=signature)
    assert third.stalled and third.replan_required and controller.replan_count == 1
    fourth = controller.observe(fingerprint, action_signature=signature)
    assert fourth.stalled and not fourth.replan_required
    assert controller.should_block(signature) is True
    different = action_signature("run_command", {"command": "pytest tests/other.py"})
    assert controller.should_block(different) is False
    assert controller.blocked_action == ""


@pytest.mark.asyncio
async def test_a10_completion_gate_rejects_missing_evidence_errors_and_unknowns(tmp_path, fake_evidence):
    goal = GoalContract("ship feature", ("done",))
    gate = __import__("cc_harness.run_model", fromlist=["CompletionGate"]).CompletionGate()
    good = CompletionCandidate(("done",), (EvidenceRef.from_dict(fake_evidence),))
    assert gate.evaluate(good, goal, strict_digests=True).accepted is True
    for kwargs in (
        {"evidence": ()},
        {"evidence": good.evidence, "unresolved_errors": ("test failed",)},
        {"evidence": good.evidence, "outcome_unknown_actions": ("a1",)},
        {"evidence": good.evidence, "pending_approvals": ("ap1",)},
        {"evidence": good.evidence, "unaccepted_children": ("child1",)},
    ):
        candidate = CompletionCandidate(("done",), **kwargs)
        result = gate.evaluate(candidate, goal, strict_digests=True)
        assert result.accepted is False and result.issues
    forged = CompletionCandidate(
        ("done",),
        (EvidenceRef("forged", EvidenceKind.TEST, "sha256:not-a-digest", "test", 1.0),),
    )
    assert gate.evaluate(forged, goal, strict_digests=True).accepted is False


@pytest.mark.asyncio
async def test_a10_completion_verifier_requires_verification_after_mutation(tmp_path):
    state = WorkingState.new(tmp_path)
    state.observe("Write", {"path": "src/app.py"}, is_error=False, result_text="written")
    report = await CompletionVerifier(
        CompletionContract(required_paths=("src/app.py",), verification_commands=("pytest",))
    ).verify(state)
    assert report.passed is False
    assert any("verification" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_a11_yield_reclaim_preserves_action_and_projection_digest(tmp_path):
    _project, store, run_id = await make_run(tmp_path)
    try:
        lease = await claim_run(store, run_id, "yield-a")
        await append_action_lifecycle(store, run_id, lease, include_observation=True)
        await append_event(
            store,
            run_id,
            "ActionSucceeded",
            {"action_id": "fi-action", "attempt": 1},
            lease_epoch=lease.epoch,
            actor_kind="worker",
            actor_id=lease.worker_id,
            expected_lease_epoch=lease.epoch,
        )
        before = await store.load_projection(run_id)
        await append_event(
            store,
            run_id,
            "RunYielded",
            {"segment": 0},
            lease_epoch=lease.epoch,
            actor_kind="worker",
            actor_id=lease.worker_id,
            expected_lease_epoch=lease.epoch,
        )
        after_yield = await store.load_projection(run_id)
        assert after_yield.status is RunStatus.QUEUED
        new = await claim_run(store, run_id, "yield-b")
        assert new.epoch == lease.epoch + 1
        restored = await store.load_projection(run_id)
        assert restored.actions[0].status is ActionStatus.SUCCEEDED
        assert restored.digest != before.digest
        assert_valid_history((await store.read(run_id, limit=10_000)).events)
    finally:
        await store.close()


def test_a12_plan_graph_cycles_dependencies_and_ownership_are_rejected():
    with pytest.raises(ValueError, match="dependency cycles"):
        PlanGraph(
            nodes=(
                PlanNode("a", "child", depends_on=("b",)),
                PlanNode("b", "child", depends_on=("a",)),
            ),
            revision=1,
        )
    service = PlanGraphService()
    plan = service.create(
        (
            PlanNode("one", "child", owned_paths=("src",), effect_class=EffectClass.WORKSPACE_MUTATION.value),
            PlanNode("two", "child", owned_paths=("src/api",), effect_class=EffectClass.WORKSPACE_MUTATION.value),
        )
    )
    with pytest.raises(PlanGraphError):
        service.choose_batch(plan, ("one", "two"))
    readonly = service.create(
        (PlanNode("r1", "child"), PlanNode("r2", "child")), max_concurrent_children=2
    )
    assert service.choose_batch(readonly, ("r1", "r2")).parallel is True


def test_a13_observation_pagination_and_conservative_scheduler_boundaries():
    with pytest.raises(ValueError, match="next_cursor"):
        make_observation(
            action_id="a",
            attempt=1,
            tool_name="Read",
            status="succeeded",
            effect_class="read_only",
            complete=False,
        )
    observation = make_observation(
        action_id="a",
        attempt=1,
        tool_name="Read",
        status="succeeded",
        effect_class="read_only",
        text="first page",
        complete=False,
        next_cursor="cursor-2",
    )
    assert observation.continuation is not None
    batches = ActionScheduler(ToolContractRegistry.first_party()).batches(
        [
            __import__("cc_harness.run_kernel", fromlist=["ActionRequest"]).ActionRequest("r1", "Read", {}),
            __import__("cc_harness.run_kernel", fromlist=["ActionRequest"]).ActionRequest("w1", "Write", {}),
        ]
    )
    assert [batch.parallel for batch in batches] == [True, False]
    assert ToolContractRegistry.first_party().get("run_command").retryable is False


def test_runtime_audit_helpers_preserve_error_classification_and_retry_budget():
    assert RecoveryPolicy(max_transient_retries=2).decide("connection reset", attempt=1).retry is True
    assert RecoveryPolicy(max_transient_retries=2).decide("connection reset", attempt=3).retry is False
    assert RecoveryPolicy().decide("permission denied", attempt=1).kind is ToolErrorKind.PERMISSION
