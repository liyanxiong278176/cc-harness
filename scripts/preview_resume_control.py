"""Isolated real-scheduler Continue fixture. No remote provider is used."""
import asyncio
import json
import tempfile
from pathlib import Path

import uvicorn

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus
from cc_harness.supervisor import LocalSupervisor
from cc_harness.tool_observation import make_observation
from cc_harness.webui import WebRuntimeManager, WebSettingsStore, create_web_app
from cc_harness.worker import ActionExecutionResult, RunWorker


async def main():
    base = Path(tempfile.mkdtemp(prefix='cc-harness-resume-verification-'))
    project = base / 'resume-workspace'
    project.mkdir()
    (project / 'README.md').write_text('# Continue verification\n', encoding='utf-8')
    gate = base / 'release-model'
    client = await DurableRuntimeClient.create(project, data_root=base / 'runtime')
    manager = WebRuntimeManager(initial_cwd=project, data_root=base / 'runtime')
    manager.settings = WebSettingsStore(base / 'settings.json')
    await manager.settings.update({'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'local-fixture-only', 'model': 'local-resume-fixture'}, project_root=project)
    manager.selected_root = project.resolve()
    manager._clients[str(project.resolve())] = client
    run_id = await client.submit('继续执行验证：读取项目说明，保留原有对话', ('README 已读取',))
    await client.coordinator.pause(run_id)
    await client.coordinator.resume(run_id, 'WebUI 用户继续')
    await client.coordinator.pause(run_id)

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            assert not any(message.get('role') == 'user' and message.get('content') in {'WebUI 用户继续', 'webui.resume'} for message in messages)
            if self.calls == 1:
                return ModelSegment(tool_calls=({'id': 'read-after-resume', 'name': 'Read', 'arguments': {'path': 'README.md'}, 'effect_class': 'read_only'},))
            async with asyncio.timeout(120):
                while not gate.exists():
                    await asyncio.sleep(0.1)
            return ModelSegment(text='项目说明已读取，当前执行步骤已推进。')

    async def execute(request):
        content = (project / 'README.md').read_text(encoding='utf-8')
        observation = make_observation(text=content, action_id=request.action_id, tool_name='Read', attempt=1, effect_class='read_only', status='succeeded')
        return ActionExecutionResult(ActionStatus.SUCCEEDED, observation=observation)

    def factory(_run_id):
        return RunWorker(client.store, ReActKernel(Model()), worker_id='continue-verification', action_executor=execute, permission_mode='bypass-prompts')

    async def start_local(**kwargs):
        await asyncio.sleep(0.25)
        if client.supervisor is None:
            client.supervisor = LocalSupervisor(client.store, factory, max_workers=1, poll_interval=0.1)
        await client.supervisor.start()
        return client.supervisor

    client.start_supervisor = start_local
    metadata = {'base_url': 'http://127.0.0.1:3082', 'run_id': run_id, 'gate': str(gate), 'project_root': str(project)}
    Path('docs/design/reference/resume-preview.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(metadata), flush=True)
    try:
        await uvicorn.Server(uvicorn.Config(create_web_app(manager), host='127.0.0.1', port=3082, log_level='warning')).serve()
    finally:
        await manager.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
