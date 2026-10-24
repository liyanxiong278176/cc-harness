from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cc_harness.artifacts import ArtifactStore
from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.followups import FollowUpService
from cc_harness.run_store import RunStore
from cc_harness.web_changes import task_changes


@pytest.mark.asyncio
async def test_unknown_effect_resume_reports_blocker_without_requeue_loop(tmp_path):
    from unittest.mock import AsyncMock
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.worker import RunWorker
    from cc_harness.webui import WebRuntimeManager
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('实现功能并验证', ('验证通过',))
        _, contracts, handlers, deps = client._build_tool_runtime()
        async def broken_handler(args, **kwargs):
            raise OSError('planning persistence interrupted after entering handler')
        handlers['task_plan'] = broken_handler
        class Model:
            async def complete(self, messages, tools):
                return ModelSegment(tool_calls=({'id': 'uncertain-plan', 'name': 'task_plan', 'effect_class': 'read_only', 'arguments': {}},))
        worker = RunWorker(client.store, ReActKernel(Model()), worker_id='unknown-regression', contracts=contracts, action_executor=lambda request: client._execute_tool(request, run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None))
        lease = await worker.claim(run_id)
        try:
            await worker.execute(lease)
        finally:
            await worker.release(lease)
        assert (await client.store.load_projection(run_id)).status.value == 'blocked'
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'data')
        manager._client_for_run = AsyncMock(return_value=client)
        with pytest.raises(ValueError, match='需要先核对结果'):
            await manager.resume(run_id)
        events = (await client.store.read(run_id, limit=1000)).events
        assert not any(event.event_type == 'RunResumed' for event in events)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,arguments', [
    ('task_plan', {'nodes': [{'node_id': 'inspect', 'kind': 'stage', 'title': '检查现有代码', 'acceptance_criteria': ['结构已核对']}], 'reason': '拆解任务'}),
    ('task_wait', {'kind': 'waiting_input', 'reason': '等待补充数据库配置'}),
])
async def test_task_control_real_dispatch_receives_current_run_id(tmp_path, name, arguments):
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import EffectClass
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        client.tool_bundles = frozenset({'core'})
        _, contracts, handlers, deps = client._build_tool_runtime()
        run_id = await client.submit('实现功能并验证', ('验证通过',))
        from cc_harness.lease import LeaseManager
        lease = await LeaseManager(client.store).claim(run_id, 'dispatch-verification')
        result = await client._execute_tool(ActionRequest('control-dispatch', name, arguments, EffectClass.READ_ONLY), run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None)
        assert result.status.value == 'succeeded'
        if name == 'task_plan':
            projection = await client.store.load_projection(run_id)
            assert projection.plan.by_id['inspect'].title == '检查现有代码'
            assert any(todo.title == '检查现有代码' for todo in projection.todos)
        await LeaseManager(client.store).release(lease)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_control_is_not_a_chat_message_and_wakes_scheduler(tmp_path):
    from unittest.mock import AsyncMock
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.webui import WebRuntimeManager, _public_event
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'data')
    manager._client_for_run = AsyncMock(return_value=client)
    manager._wake_supervisor = AsyncMock()
    client.start_supervisor = AsyncMock()
    try:
        root = await client.submit('开发图书管理系统', ('完成验证',))
        await client.coordinator.pause(root)
        result = await manager.resume(root)
        assert result['status'] == 'queued'
        manager._wake_supervisor.assert_awaited_once_with(client)
        events = (await client.store.read(root, limit=100)).events
        resumed = next(event for event in events if event.event_type == 'RunResumed')
        public = _public_event(client, resumed, root_run_id=root)
        assert public['kind'] == 'status'
        assert not public.get('content')
        assert not any(event.event_type == 'FollowUpQueued' for event in events)
        from cc_harness.interaction_history import materialize_interaction_messages
        history = await materialize_interaction_messages(client.store, await client.store.load_projection(root))
        assert not any(message.get('role') == 'user' and message.get('content') in {'webui.resume', 'WebUI 用户继续'} for message in history)
        assert any(message.get('_cc_harness_resume_control') for message in history)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_resume_start_does_not_close_the_winning_model_client(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.run_store import SupervisorLeaseConflict
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    class StartingSupervisor:
        is_running = False
        starting = False
        async def start(self):
            if self.starting:
                raise SupervisorLeaseConflict('duplicate concurrent startup')
            self.starting = True
            await asyncio.sleep(0.02)
            self.is_running = True
        async def stop(self, **kwargs):
            self.is_running = False
    supervisor = StartingSupervisor()
    client.supervisor = supervisor
    client._close_execution_components = AsyncMock()
    try:
        results = await asyncio.gather(client.start_supervisor(), client.start_supervisor(), return_exceptions=True)
        assert results == [supervisor, supervisor]
        client._close_execution_components.assert_not_awaited()
    finally:
        client.supervisor = None
        await client.close()


@pytest.mark.asyncio
async def test_planning_tools_are_exposed_in_default_core_model_tools(tmp_path):
    from cc_harness.durable_runtime import DurableRuntimeClient
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        client.tool_bundles = frozenset({'core'})
        specs, _, _, _ = client._build_tool_runtime()
        names = {spec['function']['name'] for spec in specs}
        assert {'task_plan', 'task_wait'} <= names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_model_task_plan_persists_named_steps_and_live_statuses(tmp_path):
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.execution_tools import task_plan
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('开发图书管理系统', ('通过接口与页面验证',))
        nodes = [
            {'node_id': 'api', 'kind': 'stage', 'title': '实现图书管理接口', 'acceptance_criteria': ['接口通过验证'], 'status': 'in_progress'},
            {'node_id': 'ui', 'kind': 'stage', 'title': '实现图书管理页面', 'acceptance_criteria': ['页面通过验证'], 'depends_on': ['api'], 'status': 'pending'},
        ]
        await task_plan({'nodes': nodes, 'reason': '根据用户需求拆解'}, coordinator=client.coordinator, run_id=run_id)
        projection = await client.store.load_projection(run_id)
        tasks = {todo.todo_id: todo for todo in projection.todos}
        assert tasks['api'].title == '实现图书管理接口'
        assert tasks['api'].status == 'in_progress'
        assert tasks['ui'].status == 'pending'
        assert projection.plan.by_id['api'].to_dict()['title'] == '实现图书管理接口'
        nodes[0]['status'], nodes[1]['status'] = 'done', 'in_progress'
        await task_plan({'nodes': nodes, 'reason': '接口已验证，开始页面'}, coordinator=client.coordinator, run_id=run_id)
        projection = await client.store.load_projection(run_id)
        tasks = {todo.todo_id: todo for todo in projection.todos}
        assert tasks['api'].status == 'done'
        assert tasks['ui'].status == 'in_progress'
        await client.store.checkpoint(run_id)
        assert (await client.store.load_projection(run_id)).plan.by_id['ui'].to_dict()['title'] == '实现图书管理页面'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_resume_preserves_task_list_instead_of_control_reason(tmp_path):
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.run_events import EventActor

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    actor = EventActor('client', 'resume-regression')
    try:
        root = await client.submit('开发图书管理系统', ('接口与页面通过验证',))
        for todo_id, title, status in [('api', '实现图书接口', 'done'), ('ui', '实现图书页面', 'running')]:
            await client.coordinator._append(root, 'TodoCreated', {'todo': {'id': todo_id, 'title': title, 'status': status}}, actor)
        await client.coordinator.cancel(root, '停止')
        receipt = await client.coordinator.resume(root, 'WebUI 用户继续')
        resumed = await client.store.load_projection(receipt.run_id)
        assert resumed.goal.objective == '开发图书管理系统'
        tasks = {todo.todo_id: todo for todo in resumed.todos}
        assert tasks['api'].title == '实现图书接口'
        assert tasks['api'].status == 'done'
        assert tasks['ui'].title == '实现图书页面'
        assert tasks['ui'].status == 'pending'
        assert tasks['ui'].active_sessions == (receipt.run_id,)
        assert all(todo.title != 'WebUI 用户继续' for todo in resumed.todos)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pending_message_revision_is_durable_and_withdrawn_message_never_runs(tmp_path):
    store = RunStore(tmp_path, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        run = await coordinator.submit(RunRequest("first task", ("done",)))
        service = FollowUpService(store)
        queued = await service.enqueue(run.run_id, "original request")
        await service.revise(run.run_id, queued.follow_up_run_id, "updated request")
        projection = await store.load_projection(run.run_id)
        assert store.artifacts.read_text(projection.queue[0].message_artifact) == "updated request"
        await service.revise(run.run_id, queued.follow_up_run_id, None)
        await coordinator.cancel(run.run_id, "test stop")
        assert await service.release_ready(run.run_id) == ()
        assert not await store.run_exists(queued.follow_up_run_id)
        assert (await store.load_projection(run.run_id)).queue[0].status == "cancelled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_release_uses_latest_message_and_started_message_cannot_be_revised(tmp_path):
    store = RunStore(tmp_path, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        run = await coordinator.submit(RunRequest("first task", ("done",)))
        service = FollowUpService(store)
        queued = await service.enqueue(run.run_id, "original request")
        old_digest = (await store.load_projection(run.run_id)).queue[0].message_artifact
        await service.revise(run.run_id, queued.follow_up_run_id, "updated request")
        await coordinator.cancel(run.run_id, "test stop")
        # A previously-read digest must not overwrite a durable queue revision.
        await service._start(run.run_id, queued.follow_up_run_id, old_digest, incomplete=True)
        child = await store.load_projection(queued.follow_up_run_id)
        assert child.goal.objective == "updated request"
        with pytest.raises(ValueError, match="已开始"):
            await service.revise(run.run_id, queued.follow_up_run_id, "too late")
        with pytest.raises(ValueError, match="已开始"):
            await service.revise(run.run_id, queued.follow_up_run_id, None)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_changes_use_immutable_tool_evidence_not_live_git_or_files(tmp_path):
    artifacts = ArtifactStore(tmp_path / "objects")
    before = artifacts.put(b"old line\n").digest
    after = artifacts.put(b"new line\n").digest
    (tmp_path / "app.py").write_text("unrelated user changes\n")
    observations = [
        {
            "metadata": {
                "workspace_change_set": {
                    "modified": [{"path": "app.py", "before": before, "after": after}],
                    "deleted": [{"path": "removed.py", "before": before}],
                }
            },
            "modified_paths": ["app.py"],
        },
        {
            "metadata": {
                "structured_preview": {
                    "path": "new.py",
                    "mode": "create_only",
                    "diff": "--- a/new.py\n+++ b/new.py\n@@ -0,0 +1 @@\n+print(1)\n",
                }
            }
        },
        {"metadata": {}, "modified_paths": ["legacy.py", "../outside.py"]},
        {
            "metadata": {
                "workspace_change_set": {
                    "modified": [
                        {"path": "missing.py", "before": "sha256:" + "a" * 64, "after": after}
                    ]
                }
            }
        },
    ]
    events = []
    for index, observation in enumerate(observations):
        digest = artifacts.put_text(json.dumps(observation)).digest
        events.append(
            SimpleNamespace(
                event_type="ToolObservationCommitted",
                event_id=f"e{index}",
                sequence=index + 1,
                occurred_at=f"2026-10-23T01:00:0{index}Z",
                payload={"observation_artifact": digest},
            )
        )

    async def read(run_id, *, after=0, limit=1000):
        return SimpleNamespace(events=[event for event in events if event.sequence > after][:limit])

    client = SimpleNamespace(
        store=SimpleNamespace(artifacts=artifacts, project_root=tmp_path, read=read)
    )
    changes = await task_changes(client, ["run"])
    by_path = {change["path"]: change for change in changes}
    assert "unrelated user changes" not in by_path["app.py"]["diff"]
    assert "-old line" in by_path["app.py"]["diff"]
    assert "+new line" in by_path["app.py"]["diff"]
    assert by_path["removed.py"]["kind"] == "deleted"
    assert by_path["removed.py"]["deletions"] == 1
    assert by_path["new.py"]["source"] == "native_tool"
    assert by_path["new.py"]["additions"] == 1
    assert by_path["legacy.py"]["diff"] is None
    assert by_path["missing.py"]["diff"] is None
    assert "../outside.py" not in by_path


def test_workspace_snapshot_keeps_user_baseline_and_bounds_content_capture(tmp_path):
    from cc_harness.durable_runtime import _workspace_snapshot

    artifacts = ArtifactStore(tmp_path / ".cc-harness" / "objects")
    (tmp_path / "app.py").write_text("existing uncommitted user change\n")
    (tmp_path / ".env").write_text("SECRET=private")
    (tmp_path / "large.py").write_bytes(b"a" * 262_145)
    (tmp_path / "binary.py").write_bytes(b"abc\x00def")
    snapshot = _workspace_snapshot(tmp_path, artifacts=artifacts)
    assert artifacts.read(snapshot["app.py"]) == (tmp_path / "app.py").read_bytes()
    for name in (".env", "large.py", "binary.py"):
        assert not artifacts.object_path(snapshot[name]).exists()


@pytest.mark.asyncio
async def test_followups_start_one_at_a_time_in_fifo_order(tmp_path):
    store = RunStore(tmp_path, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        run = await coordinator.submit(RunRequest("first task", ("done",)))
        service = FollowUpService(store)
        first = await service.enqueue(run.run_id, "first queued message")
        second = await service.enqueue(run.run_id, "second queued message")
        await coordinator.cancel(run.run_id, "test stop")
        released = await service.release_ready(run.run_id)
        assert [item.follow_up_run_id for item in released] == [first.follow_up_run_id]
        assert not await store.run_exists(second.follow_up_run_id)
        assert await service.release_ready(run.run_id) == ()
        await coordinator.cancel(first.follow_up_run_id, "finish first child")
        released = await service.release_ready(run.run_id)
        assert [item.follow_up_run_id for item in released] == [second.follow_up_run_id]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_web_messages_stay_on_root_fifo_while_followup_child_is_active(tmp_path):
    from unittest.mock import AsyncMock
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.webui import WebRuntimeManager

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "data")
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "data")
    manager._client_for_run = AsyncMock(return_value=client)
    manager._wake_supervisor = AsyncMock()
    try:
        run_id = await client.submit("first task", ("done",))
        await client.coordinator.cancel(run_id, "test stop")
        first = await client.coordinator.send(run_id, "first followup", follow_up=True)
        result = await manager.send_message("second followup", session_id=run_id)
        root = await client.store.load_projection(run_id)
        child = await client.store.load_projection(first.follow_up_run_id)
        assert len(root.queue) == 2
        assert root.queue[1].follow_up_run_id == result["follow_up_run_id"]
        assert root.queue[1].status == "queued"
        assert not child.queue
        assert not await client.store.run_exists(result["follow_up_run_id"])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_task_projection_follows_started_followup(tmp_path):
    """A greeting's completed todo must not become the next request's plan."""
    from unittest.mock import AsyncMock
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.webui import WebRuntimeManager

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'data')
    manager._client_for_run = AsyncMock(return_value=client)
    manager.context = AsyncMock(return_value={})
    try:
        root_id = await client.submit('你好', ('回复问候',))
        await client.coordinator.cancel(root_id, 'first turn ended')
        followup = await client.coordinator.send(root_id, '开发图书管理系统', follow_up=True)
        state = await manager.session(root_id)
        assert state['projection']['goal']['objective'] == '你好'
        assert state['task_run_id'] == followup.follow_up_run_id
        assert state['task_projection']['run_id'] == followup.follow_up_run_id
        assert state['task_projection']['goal']['objective'] == '开发图书管理系统'
        assert all(todo['title'] != '你好' for todo in state['task_projection']['todos'])
    finally:
        await client.close()
