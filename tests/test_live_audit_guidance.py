import pytest
from unittest.mock import AsyncMock

from cc_harness.run_model import RunStateMachine, RunStatus
from cc_harness.task_execution import TaskControl, TaskLifecycle


@pytest.mark.parametrize('status,lifecycle,scheduling', [
    (RunStatus.BLOCKED, TaskLifecycle.WAITING_INPUT, 'idle'),
    (RunStatus.PAUSED, TaskLifecycle.PAUSED, 'idle'),
    (RunStatus.STALLED, TaskLifecycle.PAUSED, 'idle'),
    (RunStatus.RUNNING, TaskLifecycle.RUNNING, 'leased'),
    (RunStatus.WAITING_EVENT, TaskLifecycle.WAITING_EVENT, 'idle'),
])
def test_superseding_stale_approval_preserves_recovery_boundary(status, lifecycle, scheduling):
    assert RunStateMachine().transition(status, 'TaskApprovalSuperseded') == status
    before = TaskControl(lifecycle=lifecycle, scheduling=scheduling, reason='requires reconciliation')
    after = before.apply('TaskApprovalSuperseded', {'approval_id': 'old', 'reason': 'task_requirements_changed'}, 20)
    assert after.lifecycle == lifecycle
    assert after.scheduling == scheduling
    assert after.reason == before.reason


def test_superseding_current_approval_requeues_waiting_task():
    assert RunStateMachine().transition(RunStatus.AWAITING_APPROVAL, 'TaskApprovalSuperseded') == RunStatus.QUEUED
    before = TaskControl(lifecycle=TaskLifecycle.WAITING_APPROVAL)
    after = before.apply('TaskApprovalSuperseded', {'approval_id': 'old', 'reason': 'task_requirements_changed'}, 20)
    assert after.lifecycle == TaskLifecycle.RUNNING
    assert after.scheduling == 'queued'


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', [False, True])
async def test_real_guidance_expires_stale_approval_without_resuming_uncertainty(tmp_path, blocked):
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.lease import LeaseManager
    from cc_harness.run_events import EventActor
    from cc_harness.webui import WebRuntimeManager

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'runtime')
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'runtime')
    manager._client_for_run = AsyncMock(return_value=client)
    manager._wake_supervisor = AsyncMock()
    actor = EventActor('worker', 'audit-test')
    try:
        run_id = await client.submit('完成开发', ('验证通过',))
        lease = await LeaseManager(client.store).claim(run_id, 'audit-test')
        if blocked:
            await client.coordinator._append(run_id, 'ActionPlanned', {
                'action_id': 'uncertain', 'tool_name': 'run_command', 'attempt': 1,
                'normalized_args_digest': 'sha256:' + 'a' * 64,
                'arguments_artifact': 'sha256:' + 'b' * 64,
                'contract_digest': 'sha256:' + 'c' * 64,
                'effect_class': 'unknown', 'worker_id': 'audit-test',
            }, actor)
            for event in ('ActionPrepared', 'ActionStarted', 'ActionOutcomeUnknown'):
                await client.coordinator._append(run_id, event, {
                    'action_id': 'uncertain', 'attempt': 1, 'reason': 'captured timeout',
                }, actor)
        await client.coordinator._append(run_id, 'ActionPlanned', {
            'action_id': 'old', 'tool_name': 'run_command', 'attempt': 1,
            'normalized_args_digest': 'sha256:' + 'a' * 64,
            'arguments_artifact': 'sha256:' + 'b' * 64,
            'contract_digest': 'sha256:' + 'c' * 64,
            'effect_class': 'unknown', 'worker_id': 'audit-test',
        }, actor)
        await client.coordinator._append(run_id, 'ApprovalRequested', {
            'approval_id': 'approval-old', 'action_id': 'old',
            'action_args_digest': 'sha256:' + 'a' * 64, 'scope': ['run_command'],
        }, actor)
        if blocked:
            recovery_actor = EventActor('client', 'audit-test-recovery')
            await client.coordinator._append(run_id, 'RunBlocked', {'reason': 'requires reconciliation'}, recovery_actor)
            await LeaseManager(client.store).release(lease)
            receipt = await manager.send_message('补充要求已收到', session_id=run_id)
            assert receipt['status'] == 'blocked'
            assert receipt['resume_required'] is True
            manager._wake_supervisor.assert_not_awaited()
        else:
            await client.coordinator.pause(run_id)
            await LeaseManager(client.store).release(lease)
            await client.coordinator.execution.guidance(run_id, '调整开发范围')
        projection = await client.store.load_projection(run_id)
        assert projection.status == (RunStatus.BLOCKED if blocked else RunStatus.PAUSED)
        assert projection.approvals[0].status.value == 'expired'
        assert len(projection.task.guidance) == 1
        events = (await client.store.read(run_id, limit=1000)).events
        assert not any(e.event_type == 'RunResumed' for e in events)
        if blocked:
            from cc_harness.interaction_history import materialize_interaction_messages
            await client.coordinator.reconcile_action(run_id, 'uncertain', 'succeeded', reason='HTTP and process evidence verified')
            messages = await materialize_interaction_messages(client.store, await client.store.load_projection(run_id))
            resolutions = [m for m in messages if m.get('_cc_harness_reconciliation')]
            assert len(resolutions) == 1
            assert 'HTTP and process evidence verified' in resolutions[0]['content']
            assert 'Do not replay' in resolutions[0]['content']
            assert resolutions[0]['_cc_harness_untrusted'] is True
    finally:
        await client.close()
