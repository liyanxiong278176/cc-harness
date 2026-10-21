from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.execution_control import BudgetExhausted
from cc_harness.kernel_middleware import KernelDecision, KernelMiddleware
from cc_harness.run_events import EventActor
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import GoalContract, Run, RunStatus
from cc_harness.run_projection import RunProjection
from cc_harness.run_store import RunStore
from cc_harness.task_execution import TaskLifecycle, budget_values
from cc_harness.worker import RunWorker


@pytest_asyncio.fixture
async def setup_task(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    coordinator = RunCoordinator(store)
    handle = await coordinator.submit(RunRequest("implement login", ("tests pass",)))
    try:
        yield store, coordinator, handle.run_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pause_is_recoverable_cancellation_creates_successor(setup_task):
    store, coordinator, run_id = setup_task
    await coordinator.pause(run_id)
    projection = await store.load_projection(run_id)
    assert projection.status is RunStatus.PAUSED
    assert projection.task.lifecycle is TaskLifecycle.PAUSED
    assert projection.task.scheduling == "idle"
    await coordinator.resume(run_id)
    assert (await store.load_projection(run_id)).status is RunStatus.QUEUED
    await coordinator.cancel(run_id, "cancel")
    receipt = await coordinator.resume(run_id, "continue from existing work")
    assert receipt.run_id != run_id
    assert (await store.load_projection(run_id)).status is RunStatus.CANCELLED
    assert (await store.load_run_record(receipt.run_id)).predecessor_run_id == run_id
    successor = await store.load_projection(receipt.run_id)
    assert successor.goal.acceptance_criteria == projection.goal.acceptance_criteria
    assert "implement login" in successor.goal.objective
    worker = RunWorker(store, ReActKernel(EmptyModel()), worker_id="successor")
    await worker.execute(await worker.claim(receipt.run_id))
    successor = await store.load_projection(receipt.run_id)
    assert successor.task.used["model_calls"] == 1
    assert "ancestor is stopped" not in successor.task.reason


@pytest.mark.asyncio
async def test_budget_reservations_serialize_and_survive_replay(setup_task):
    store, coordinator, run_id = setup_task
    execution = coordinator.execution
    await execution.configure_budget(run_id, {"tool_calls": 1})
    results = await asyncio.gather(
        execution.reserve(run_id, {"tool_calls": 1}),
        execution.reserve(run_id, {"tool_calls": 1}), return_exceptions=True,
    )
    keys = [x for x in results if isinstance(x, str)]
    assert len(keys) == 1
    assert sum(isinstance(x, BudgetExhausted) for x in results) == 1
    await execution.settle(run_id, keys[0], {"tool_calls": 1})
    await execution.settle(run_id, keys[0], {"tool_calls": 1})
    projection = await store.load_projection(run_id)
    assert projection.task.used == {"tool_calls": 1}
    assert RunProjection.from_dict(projection.to_dict()).digest == projection.digest
    with pytest.raises(ValueError, match="increase"):
        await coordinator.resume(run_id)
    await execution.configure_budget(run_id, {"tool_calls": 2})
    await coordinator.resume(run_id)
    assert (await store.load_projection(run_id)).task.used["tool_calls"] == 1


@pytest.mark.asyncio
async def test_child_consumption_is_charged_to_parent(setup_task):
    store, coordinator, run_id = setup_task
    parent = await store.load_projection(run_id)
    child_id = str(uuid.uuid4())
    goal = GoalContract("child", ("done",))
    await store.create_run(Run(child_id, goal, parent.runtime_contract, parent_run_id=run_id))
    actor = EventActor("client", "test")
    await coordinator._append(child_id, "RunCreated", {
        "goal": goal.to_dict(), "runtime_contract": parent.runtime_contract.to_dict(),
    }, actor)
    await coordinator._append(child_id, "RunQueued", {}, actor)
    await coordinator.execution.configure_budget(run_id, {"tokens": 10})
    key = await coordinator.execution.reserve(child_id, {"tokens": 6})
    await coordinator.execution.settle(child_id, key, {"tokens": 5})
    assert (await store.load_projection(child_id)).task.used["tokens"] == 5
    assert (await store.load_projection(run_id)).task.used["tokens"] == 5
    with pytest.raises(BudgetExhausted):
        await coordinator.execution.reserve(child_id, {"tokens": 6})


@pytest.mark.asyncio
async def test_guidance_updates_current_goal_once(setup_task):
    store, coordinator, run_id = setup_task
    receipt = await coordinator.send(run_id, "验证码改成6位")
    assert receipt.follow_up_run_id is None
    assert await coordinator.execution.apply_guidance(run_id)
    assert not await coordinator.execution.apply_guidance(run_id)
    projection = await store.load_projection(run_id)
    assert "验证码改成6位" in projection.goal.objective
    assert projection.goal.contract_version == 2
    assert projection.task.guidance[0]["applied"]
    assert not projection.queue


@pytest.mark.asyncio
async def test_wait_event_is_matched_and_cannot_override_pause(setup_task):
    store, coordinator, run_id = setup_task
    worker = RunWorker(store, ReActKernel(EmptyModel()), worker_id="wait")
    lease = await worker.claim(run_id)
    wait_id = await coordinator.execution.wait(run_id, kind="waiting_event", reason="test job")
    await worker.release(lease)
    assert not await coordinator.execution.wake(run_id, "unrelated")
    await coordinator.pause(run_id)
    assert not await coordinator.execution.wake(run_id, wait_id)
    assert (await store.load_projection(run_id)).status is RunStatus.PAUSED


class EmptyModel:
    async def complete(self, messages, tools):
        return ModelSegment(text="need more evidence")


@pytest.mark.asyncio
async def test_zero_steps_stops_before_calling_model(setup_task):
    store, coordinator, run_id = setup_task
    class NeverModel:
        async def complete(self, messages, tools):
            pytest.fail("budget exhausted task called a model")
    await coordinator.execution.configure_budget(run_id, {"steps": 0})
    worker = RunWorker(store, ReActKernel(NeverModel()), worker_id="budget")
    await worker.execute(await worker.claim(run_id))
    projection = await store.load_projection(run_id)
    assert projection.status is RunStatus.PAUSED
    assert "budget_exhausted" in projection.task.reason


@pytest.mark.asyncio
async def test_running_pause_commits_at_safe_boundary(setup_task):
    store, coordinator, run_id = setup_task
    entered, finish = asyncio.Event(), asyncio.Event()
    class Model:
        async def complete(self, messages, tools):
            entered.set()
            await finish.wait()
            return ModelSegment(text="finished reasoning")
    worker = RunWorker(store, ReActKernel(Model()), worker_id="pause")
    task = asyncio.create_task(worker.execute(await worker.claim(run_id)))
    await asyncio.wait_for(entered.wait(), 5)
    await coordinator.pause(run_id)
    assert (await store.load_projection(run_id)).task.pause_requested
    finish.set()
    await asyncio.wait_for(task, 5)
    assert (await store.load_projection(run_id)).status is RunStatus.PAUSED


@pytest.mark.asyncio
async def test_terminal_loss_pauses_without_cancelling(setup_task, monkeypatch):
    store, coordinator, run_id = setup_task
    monkeypatch.setattr("cc_harness.terminal_owner.process_identity", lambda pid: "birth-1")
    await coordinator.execution.attach_terminal(run_id, 1234)
    monkeypatch.setattr("cc_harness.terminal_owner.process_identity", lambda pid: "birth-2")
    await coordinator.execution.inspect_waits_and_terminals()
    projection = await store.load_projection(run_id)
    assert projection.status is RunStatus.PAUSED
    assert projection.task.reason == "terminal_closed"


@pytest.mark.asyncio
async def test_plan_approval_is_bound_to_current_digest(setup_task):
    store, coordinator, run_id = setup_task
    await coordinator.execution.append(run_id, "TaskPlanApprovalRequired", {})
    projection = await store.load_projection(run_id)
    with pytest.raises(ValueError, match="stale"):
        await coordinator.approve_plan(run_id, "old")
    await coordinator.approve_plan(run_id, projection.plan.digest)
    assert (await store.load_projection(run_id)).mutation_gate == "open"


@pytest.mark.asyncio
async def test_middleware_order_conflict_and_short_circuit():
    calls = []
    chain = KernelMiddleware()
    async def first(point, context):
        calls.append("first")
        return KernelDecision(patch={"annotations": {"verified": True}})
    async def stop(point, context):
        calls.append("stop")
        assert context["annotations"]["verified"]
        return KernelDecision(False, "pause")
    chain.register("stop", stop, order=2)
    chain.register("first", first, order=1, writes=frozenset({"annotations"}))
    assert not (await chain.run("act.before", {})).allow
    assert calls == ["first", "stop"]
    async def conflicting(point, context):
        return KernelDecision(patch={"annotations": {"verified": False}})
    other = KernelMiddleware()
    other.register("first", first, writes=frozenset({"annotations"}))
    other.register("second", conflicting, order=1, writes=frozenset({"annotations"}))
    with pytest.raises(ValueError, match="conflicting"):
        await other.run("model.before", {})


@pytest.mark.parametrize("values", [{"tokens": -1}, {"tokens": float("nan")},
                                    {"tokens": True}, {"unknown": 3}])
def test_budget_values_reject_invalid_inputs(values):
    with pytest.raises(ValueError):
        budget_values(values)


@pytest.mark.asyncio
async def test_guidance_during_model_discards_old_tool_proposal(setup_task):
    store, coordinator, run_id = setup_task
    entered, finish = asyncio.Event(), asyncio.Event()
    executed = []
    class Model:
        calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await finish.wait()
                return ModelSegment(tool_calls=({"id": "old", "name": "Read", "arguments": {"path": "old"}},))
            assert any("6位" in str(message) for message in messages)
            return ModelSegment(text="updated plan")
    async def execute(request):
        executed.append(request)
        raise AssertionError("stale action must not execute")
    worker = RunWorker(store, ReActKernel(Model()), worker_id="guide", action_executor=execute)
    task = asyncio.create_task(worker.execute(await worker.claim(run_id)))
    await asyncio.wait_for(entered.wait(), 5)
    await coordinator.send(run_id, "验证码改为6位")
    finish.set()
    await asyncio.wait_for(task, 5)
    assert not executed
    assert "6位" in (await store.load_projection(run_id)).goal.objective


@pytest.mark.asyncio
async def test_batch_budget_failure_leaves_no_unmatched_calls(setup_task):
    store, coordinator, run_id = setup_task
    class Model:
        async def complete(self, messages, tools):
            return ModelSegment(tool_calls=tuple(
                {"id": f"read-{i}", "name": "Read", "arguments": {"path": str(i)}} for i in range(2)
            ))
    await coordinator.execution.configure_budget(run_id, {"tool_calls": 1})
    worker = RunWorker(store, ReActKernel(Model()), worker_id="batch-budget")
    await worker.execute(await worker.claim(run_id))
    projection = await store.load_projection(run_id)
    assert projection.status is RunStatus.PAUSED
    assert not projection.task.reservations
    assert not projection.actions
    assert projection.task.used.get("tool_calls", 0) == 0
    assert not any(event.event_type == "AssistantMessageCommitted" for event in (await store.read(run_id)).events)


@pytest.mark.asyncio
async def test_progress_question_does_not_change_goal(setup_task):
    store, coordinator, run_id = setup_task
    before = await store.load_projection(run_id)
    await coordinator.send(run_id, "做到哪了？")
    after = await store.load_projection(run_id)
    assert before.goal.digest == after.goal.digest
    assert not after.task.guidance
    assert any(event.event_type == "TaskProgressReported" for event in (await store.read(run_id)).events)


@pytest.mark.asyncio
async def test_input_wait_wakes_on_user_response(setup_task):
    store, coordinator, run_id = setup_task
    worker = RunWorker(store, ReActKernel(EmptyModel()), worker_id="input")
    lease = await worker.claim(run_id)
    await coordinator.execution.wait(run_id, kind="waiting_input", reason="choose version")
    await worker.release(lease)
    await coordinator.send(run_id, "use version 2")
    assert (await store.load_projection(run_id)).status is RunStatus.QUEUED


@pytest.mark.asyncio
async def test_legacy_migration_preserves_identity_and_is_idempotent(setup_task):
    store, coordinator, _ = setup_task
    from cc_harness.run_events import RunEvent
    from cc_harness.run_model import RuntimeContract
    legacy_id = str(uuid.uuid4())
    goal = GoalContract("old task", ("done",))
    contract = RuntimeContract("legacy", 1, "tools", "model", "policy", "capabilities")
    await store.create_run(Run(legacy_id, goal, contract))
    await store.append(RunEvent.create(run_id=legacy_id, sequence=1, event_type="RunCreated",
        actor=EventActor("client", "legacy"), runtime_contract_digest=contract.digest,
        payload={"goal": goal.to_dict(), "runtime_contract": contract.to_dict()}))
    await coordinator.execution.migrate_legacy(legacy_id)
    before = await store.load_projection(legacy_id)
    await coordinator.execution.migrate_legacy(legacy_id)
    after = await store.load_projection(legacy_id)
    assert before.digest == after.digest
    assert after.goal.digest == goal.digest
    assert after.status is RunStatus.PAUSED
    assert after.runtime_contract_digest != contract.digest


@pytest.mark.asyncio
async def test_active_time_budget_stops_slow_model(setup_task):
    store, coordinator, run_id = setup_task
    class Slow:
        async def complete(self, messages, tools):
            await asyncio.sleep(30)
            return ModelSegment(text="too late")
    await coordinator.execution.configure_budget(run_id, {"active_seconds": 0.05})
    worker = RunWorker(store, ReActKernel(Slow()), worker_id="time-budget")
    await asyncio.wait_for(worker.execute(await worker.claim(run_id)), 5)
    projection = await store.load_projection(run_id)
    assert projection.status is RunStatus.PAUSED
    assert "active_seconds" in projection.task.reason
