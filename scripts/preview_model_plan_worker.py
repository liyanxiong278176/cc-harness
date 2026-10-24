"""Deterministic model -> task_plan -> durable events fixture (no remote calls)."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus
from cc_harness.tool_observation import make_observation
from cc_harness.worker import ActionExecutionResult, RunWorker


async def wait_for(path: Path):
    async with asyncio.timeout(60):
        while not path.exists():
            await asyncio.sleep(0.1)


async def main():
    project = Path(sys.argv[1])
    client = await DurableRuntimeClient.create(project, data_root=project.parent / 'runtime')
    client.tool_bundles = frozenset({'core'})
    tool_specs, contracts, handlers, deps = client._build_tool_runtime()
    run_id = await client.submit('开发图书管理系统，使用 Vue3、Spring Boot 和 MySQL', ('完成并验证图书管理功能',))
    prefix = project.parent / f'model-plan-{uuid.uuid4().hex}'
    start, advance = prefix.with_suffix('.start'), prefix.with_suffix('.advance')
    print(json.dumps({'run_id': run_id, 'start': str(start), 'advance': str(advance)}), flush=True)
    nodes = [
        {'node_id': 'inspect', 'kind': 'stage', 'title': '检查项目结构与现有接口', 'acceptance_criteria': ['读取并核对项目说明'], 'status': 'in_progress'},
        {'node_id': 'implement', 'kind': 'stage', 'title': '实现图书接口与管理页面', 'acceptance_criteria': ['图书查询与编辑可用'], 'depends_on': ['inspect'], 'status': 'pending'},
        {'node_id': 'verify', 'kind': 'stage', 'title': '验证接口、页面与数据库连接', 'acceptance_criteria': ['验证通过并记录结果'], 'depends_on': ['implement'], 'status': 'pending'},
    ]

    class LocalModel:
        calls = 0

        async def complete(self, _messages, _tools):
            assert 'task_plan' in {tool['function']['name'] for tool in _tools}
            self.calls += 1
            if self.calls == 2:
                print('PLAN_READY', flush=True)
                await wait_for(advance)
                return ModelSegment(tool_calls=({'id': 'read-project', 'name': 'Read', 'effect_class': 'read_only', 'arguments': {'path': 'README.md'}},))
            if self.calls == 3:
                nodes[0]['status'], nodes[1]['status'] = 'done', 'in_progress'
            if self.calls in {1, 3}:
                return ModelSegment(text='先核对项目结构，再实现功能并验证。' if self.calls == 1 else '已核对项目说明，开始实现图书管理功能。', tool_calls=({
                    'id': f'plan-{self.calls}', 'name': 'task_plan', 'effect_class': 'read_only',
                    'arguments': {'nodes': nodes, 'reason': '拆解当前需求' if self.calls == 1 else '项目说明已实际读取并核对'},
                },))
            return ModelSegment(text='等待下一个执行阶段。')

    async def execute(request):
        if request.tool_name == 'task_plan':
            return await client._execute_tool(request, run_id=run_id, handlers=handlers, handler_deps=deps, contracts=contracts, l5=None)
        else:
            content = (project / 'README.md').read_text(encoding='utf-8')
        observation = make_observation(text=content, attempt=1, effect_class='read_only', action_id=request.action_id, tool_name=request.tool_name, status='succeeded')
        return ActionExecutionResult(ActionStatus.SUCCEEDED, observation=observation)

    try:
        await wait_for(start)
        worker = RunWorker(client.store, ReActKernel(LocalModel()), worker_id='model-plan-verification', action_executor=execute, available_tools=tool_specs, contracts=contracts, permission_mode='bypass-prompts')
        lease = await worker.claim(run_id)
        try:
            await worker.execute(lease)
        finally:
            await worker.release(lease)
        projection = await client.store.load_projection(run_id)
        todos = {todo.todo_id: todo for todo in projection.todos}
        assert todos['inspect'].status == 'done'
        assert todos['implement'].status == 'in_progress'
        assert todos['implement'].title == '实现图书接口与管理页面'
        print('UPDATE_READY', flush=True)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
