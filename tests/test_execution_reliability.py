from unittest.mock import AsyncMock

import httpx
import pytest

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.followups import FollowUpService
from cc_harness.webui import WebRuntimeManager, create_web_app


@pytest.mark.asyncio
async def test_partial_startup_failure_cleans_resources_and_allows_retry(tmp_path):
    from types import SimpleNamespace
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    provider = SimpleNamespace(aclose=AsyncMock())
    async def broken(**kwargs):
        client._llm = provider
        raise OSError('local capability initialization failed')
    client._start_supervisor_unlocked = broken
    try:
        with pytest.raises(OSError):
            await client.start_supervisor()
        provider.aclose.assert_awaited_once()
        assert client._llm is None
        # The control-plane store must survive a failed startup.
        assert await client.submit('retry after local startup failure', ('verified',))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_one_cleanup_failure_does_not_skip_other_owned_resources(tmp_path):
    from types import SimpleNamespace
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    provider = SimpleNamespace(aclose=AsyncMock())
    capabilities = SimpleNamespace(close=AsyncMock())
    client._mcp = SimpleNamespace(shutdown=AsyncMock(side_effect=OSError('local connection already lost')))
    client._llm = provider
    client._capabilities = capabilities
    try:
        await client._close_execution_components()
        provider.aclose.assert_awaited_once()
        capabilities.close.assert_awaited_once()
        assert client._mcp is None
        assert client._llm is None
    finally:
        client._mcp = None
        await client.close()


@pytest.mark.asyncio
async def test_command_can_request_bounded_build_timeout(tmp_path):
    import sys
    from cc_harness.executor import NativeExecutor
    from cc_harness.run_kernel import ActionRequest, ModelSegment, ReActKernel
    from cc_harness.run_model import EffectClass
    from cc_harness.worker import RunWorker
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    executor = NativeExecutor(tmp_path, timeout_s=0.1)
    try:
        command = 'Start-Sleep -Milliseconds 200; Write-Output OK' if sys.platform == 'win32' else 'sleep 0.2; echo OK'
        args = {'command': command, 'timeout_seconds': 3}
        result = await executor.run(args, cwd=tmp_path)
        assert not result.is_error, result.llm_text
        assert 'OK' in result.llm_text
        class Model:
            async def complete(self, messages, tools):
                return ModelSegment(text='done')
        worker = RunWorker(client.store, ReActKernel(Model()), worker_id='timeout-test')
        request = ActionRequest('build-timeout', 'run_command', args, EffectClass.UNKNOWN)
        assert worker._action_timeout_for(request, worker.contracts.get('run_command')) >= 13
    finally:
        await executor.kill()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['cancel', 'telemetry'])
async def test_native_command_stops_process_after_local_interruption(tmp_path, monkeypatch, fault):
    import asyncio
    import sys
    import cc_harness.executor as module
    created = []
    ready = asyncio.Event()
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        ready.set()
        return proc
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    if fault == 'telemetry':
        async def broken(*args):
            raise RuntimeError('injected telemetry adapter failure')
        monkeypatch.setattr(module, '_async_process_activity_snapshot', broken)
    command = 'Start-Sleep -Seconds 20' if sys.platform == 'win32' else 'sleep 20'
    task = asyncio.create_task(module.NativeExecutor(tmp_path, timeout_s=30).run({'command': command}, cwd=tmp_path))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        if fault == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.is_error
        assert created[0].returncode is not None
    finally:
        for proc in created:
            if proc.returncode is None:
                await module._terminate_process_tree(proc)


@pytest.mark.asyncio
async def test_missing_command_executor_is_known_pre_dispatch_failure(tmp_path):
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import EffectClass
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('check command', ('verified',))
        _, contracts, handlers, deps = client._build_tool_runtime()
        result = await client._execute_tool(ActionRequest('missing-executor', 'run_command', {'command': 'echo test'}, EffectClass.UNKNOWN), run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None)
        assert result.status.value == 'failed'
        assert result.observation.metadata['execution_not_started'] is True
    finally:
        await client.close()


def test_windows_process_telemetry_is_thread_safe():
    import os
    if os.name != 'nt':
        pytest.skip('Windows process metrics')
    from concurrent.futures import ThreadPoolExecutor
    from cc_harness.executor import _windows_process_metrics
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_windows_process_metrics, [os.getpid()] * 250))
    assert all(cpu >= 0 and io >= 0 for cpu, io in results)


@pytest.mark.asyncio
async def test_refresh_restarts_dead_scheduler_handle(tmp_path):
    from types import SimpleNamespace
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        await client.submit('queued work after scheduler exit', ('verified',))
        client.supervisor = SimpleNamespace(is_running=False)
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'data')
        manager._clients[str(tmp_path.resolve())] = client
        manager.settings.effective = AsyncMock(return_value={'base_url': 'http://local.invalid', 'model': 'local', 'api_key': 'test-only'})
        manager._client_for_root = AsyncMock(return_value=client)
        await manager._start_pending_if_configured(tmp_path)
        manager._client_for_root.assert_awaited_once_with(tmp_path, start=True)
    finally:
        client.supervisor = None
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['Read', 'Write'])
@pytest.mark.parametrize('interruption', ['pause', 'timeout'])
async def test_interrupted_read_is_recoverable_but_write_requires_reconciliation(tmp_path, tool, interruption):
    import asyncio
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.worker import RunWorker

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    task = None
    try:
        run_id = await client.submit('implement and verify task', ('verified',))
        entered = asyncio.Event()
        class Model:
            calls = 0
            async def complete(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return ModelSegment(tool_calls=({'id': 'interrupted', 'name': tool, 'arguments': {'path': 'test.txt'}},))
                return ModelSegment(text='await next instruction')
        async def executor(request):
            entered.set()
            await asyncio.Event().wait()
        worker = RunWorker(client.store, ReActKernel(Model()), worker_id='interruption-test', permission_mode='bypass-prompts', action_executor=executor, action_timeout_seconds=0.1 if interruption == 'timeout' else 30)
        lease = await worker.claim(run_id)
        task = asyncio.create_task(worker.execute(lease))
        await asyncio.wait_for(entered.wait(), 2)
        if interruption == 'pause':
            await client.coordinator.pause(run_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            await asyncio.wait_for(task, 3)
        projection = await client.store.load_projection(run_id)
        unknown = [action for action in projection.actions if action.status.value == 'outcome_unknown']
        assert bool(unknown) is (tool == 'Write')
        if tool == 'Read':
            assert projection.status.value in {'paused', 'stalled', 'queued'}
            await client.coordinator.resume(run_id)
            assert (await client.store.load_projection(run_id)).status.value == 'queued'
    finally:
        if task and not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
async def test_project_executor_calls_and_cleanup_are_isolated(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from cc_harness.executor_session import ExecutorSession, bind_executor_session
    from cc_harness.tools import get_session_executor
    from cc_harness.mcp_client import ToolResult
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import EffectClass

    clients = []
    try:
        for index in range(2):
            project = tmp_path / str(index)
            project.mkdir()
            client = await DurableRuntimeClient.create(project, data_root=tmp_path / 'data')
            # Exercise the actual runtime tool and cleanup entrypoints, with
            # distinct executable resources in the same process.
            client._executor_session = ExecutorSession(executor=SimpleNamespace(owner=index, kill=AsyncMock()))
            client._execution_started = True
            clients.append(client)
        async def check(client):
            _, contracts, handlers, deps = client._build_tool_runtime()
            async def read(args, **kwargs):
                await asyncio.sleep(0)
                return ToolResult.success(str(get_session_executor().owner))
            handlers['Read'] = read
            run_id = await client.submit('inspect project', ('verified',))
            result = await client._execute_tool(ActionRequest('owner-check', 'Read', {}, EffectClass.READ_ONLY), run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None)
            assert result.status.value == 'succeeded'
            assert result.observation.text == str(clients.index(client))
        await asyncio.gather(*(check(client) for client in clients))
        first_executor = clients[0]._executor_session.executor
        second_executor = clients[1]._executor_session.executor
        await clients[0]._close_execution_components()
        first_executor.kill.assert_awaited_once()
        second_executor.kill.assert_not_awaited()
        with bind_executor_session(clients[1]._executor_session):
            assert get_session_executor() is second_executor
        await check(clients[1])
    finally:
        for client in clients:
            await client.close()


@pytest.mark.asyncio
async def test_pre_execution_snapshot_error_releases_command_lock(tmp_path, monkeypatch):
    import asyncio
    import cc_harness.durable_runtime as runtime
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import EffectClass

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('run verification', ('verified',))
        _, contracts, handlers, deps = client._build_tool_runtime()
        def unavailable(*args, **kwargs):
            raise PermissionError('workspace snapshot temporarily unavailable')
        monkeypatch.setattr(runtime, '_workspace_snapshot', unavailable)
        request = ActionRequest('snapshot-fault', 'run_command', {'command': 'echo test'}, EffectClass.UNKNOWN)
        for _ in range(2):
            result = await asyncio.wait_for(client._execute_tool(request, run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None), 1)
            assert result.status.value == 'failed'
            assert not client._workspace_command_lock.locked()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_worker_factory_failure_does_not_starve_other_tasks(tmp_path):
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.supervisor import LocalSupervisor
    from cc_harness.worker import RunWorker

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    supervisor = None
    try:
        broken = await client.submit('broken task configuration', ('verified',))
        healthy = await client.submit('healthy independent task', ('verified',))
        class Model:
            async def complete(self, messages, tools):
                return ModelSegment(text='inspection completed')
        def factory(run_id):
            if run_id == broken:
                raise ValueError('local worker configuration unavailable')
            return RunWorker(client.store, ReActKernel(Model()), worker_id='healthy-worker')
        supervisor = LocalSupervisor(client.store, factory, max_workers=2)
        await supervisor.tick()
        assert healthy in supervisor._active
        assert (await client.store.load_projection(broken)).status.value == 'paused'
        assert 'worker_initialization_failed' in (await client.store.load_projection(broken)).task.reason
    finally:
        if supervisor:
            await supervisor.stop()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['binding', 'read', 'cancel', 'mutation'])
async def test_tool_failure_respects_execution_boundary(tmp_path, failure):
    import asyncio
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import EffectClass

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        run_id = await client.submit('implement task', ('verified',))
        _, contracts, handlers, deps = client._build_tool_runtime()
        name = 'Read' if failure == 'read' else 'Write'
        invoked = []

        async def handler(args, **kwargs):
            invoked.append(True)
            if failure == 'cancel':
                raise asyncio.CancelledError()
            raise OSError('injected local I/O failure')

        if failure == 'binding':
            async def handler(args, *, missing_dependency):
                invoked.append(True)
                raise AssertionError('handler must not run')
        handlers[name] = handler
        request = ActionRequest('fault-boundary', name, {}, EffectClass.READ_ONLY if name == 'Read' else EffectClass.WORKSPACE_MUTATION)
        call = client._execute_tool(request, run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None)
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            result = await call
            assert result.status.value == ('outcome_unknown' if failure == 'mutation' else 'failed')
            if failure != 'mutation':
                assert result.observation is not None
                assert result.observation.status == 'failed'
            if failure == 'binding':
                assert not invoked
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['pause', 'budget', 'wake', 'plan/approve'])
async def test_conversation_controls_reach_followup_owner(tmp_path, control):
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / 'data')
    try:
        root = await client.submit('first task', ('done',))
        await client.coordinator.cancel(root, 'finished initial interaction')
        queued = await client.coordinator.send(root, 'implement next task', follow_up=True)
        await FollowUpService(client.store).release_ready(root)
        child = queued.follow_up_run_id
        manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / 'data')
        manager._validate_root_run = AsyncMock(return_value=client)
        manager._wake_supervisor = AsyncMock()
        manager._client_for_root = AsyncMock(return_value=client)
        payload = {}
        method = 'POST'
        if control == 'budget':
            method, payload = 'PUT', {'limits': {'tool_calls': 200}}
        elif control == 'wake':
            from cc_harness.lease import LeaseManager
            lease = await LeaseManager(client.store).claim(child, 'wait-test')
            await client.coordinator.execution.wait(child, kind='waiting_input', reason='configuration', wait_id='child-wait')
            await LeaseManager(client.store).release(lease)
            payload = {'wait_id': 'child-wait'}
        elif control == 'plan/approve':
            from cc_harness.lease import LeaseManager
            lease = await LeaseManager(client.store).claim(child, 'plan-approval-test')
            await client.coordinator.execution.append(child, 'TaskPlanApprovalRequired', {})
            await client.coordinator.execution.wait(child, kind='waiting_input', reason='approve plan', wait_id='plan-wait')
            await LeaseManager(client.store).release(lease)
            payload = {'plan_digest': (await client.store.load_projection(child)).plan.digest}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_web_app(manager)), base_url='http://test') as api:
            response = await api.request(method, f'/api/sessions/{root}/{control}', json=payload)
        assert response.status_code == 200, response.text
        if control == 'pause':
            assert (await client.store.load_projection(child)).status.value == 'paused'
        elif control == 'budget':
            assert (await client.store.load_projection(child)).task.limits['tool_calls'] == 200
        elif control == 'wake':
            assert response.json()['resumed'] is True
            assert (await client.store.load_projection(child)).status.value == 'queued'
            manager._wake_supervisor.assert_awaited_once_with(client)
            manager._client_for_root.assert_awaited_once_with(client.cwd, start=True)
        else:
            projection = await client.store.load_projection(child)
            assert projection.task.approved_plan_digest == payload['plan_digest']
            assert projection.status.value == 'queued'
            manager._client_for_root.assert_awaited_once_with(client.cwd, start=True)
    finally:
        await client.close()
