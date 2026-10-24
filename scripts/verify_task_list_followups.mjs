/** Cross-request regression: real durable follow-ups and SSE, no page reload. */
import { createRequire } from 'node:module'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const exec = promisify(execFile)
const metadata = JSON.parse(await readFile('docs/design/reference/redesign-preview.json', 'utf8'))
const directory = path.resolve('docs/design/screenshots/2026-10-24')
await mkdir(directory, { recursive: true })
async function change(mode, id = '') {
  const code = `import asyncio,json,sys
from pathlib import Path
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.run_events import EventActor
from cc_harness.lease import LeaseManager
async def main():
    project=Path(sys.argv[1])
    client=await DurableRuntimeClient.create(project,data_root=project.parent/'runtime')
    actor=EventActor('client','followup-task-verification')
    mode,run_id=sys.argv[2:4]
    try:
        if mode in ('create','resume-create'):
            run_id=await client.submit('继续开发图书借阅功能' if mode=='resume-create' else '你好',('回复问候',))
            view=await client.store.load_projection(run_id)
            for todo in view.todos:
                if mode=='create':
                    await client.coordinator._append(run_id,'TodoCompleted',{'todo_id':todo.todo_id},actor)
            if mode=='resume-create':
                for todo_id,title,status in [('resume-api','实现借阅接口','done'),('resume-ui','实现借阅页面','running'),('resume-tests','验证借阅与归还流程','pending')]:
                    await client.coordinator._append(run_id,'TodoCreated',{'todo':{'id':todo_id,'title':title,'status':status}},actor)
                from cc_harness.run_kernel import ModelSegment,ReActKernel
                from cc_harness.worker import RunWorker,ActionExecutionResult
                from cc_harness.run_model import ActionStatus
                from cc_harness.tool_observation import make_observation
                class LocalReadModel:
                    calls=0
                    async def complete(self,messages,tools):
                        self.calls+=1
                        return ModelSegment(tool_calls=({'id':'read-before-stop','name':'Read','arguments':{'path':'README.md'}},)) if self.calls==1 else ModelSegment(text='已读取项目说明，待继续实现页面。')
                async def read_project(request):
                    content=(project/'README.md').read_text(encoding='utf-8')
                    observation=make_observation(text=content,attempt=1,effect_class=str(request.effect_class),action_id=request.action_id,tool_name=request.tool_name,status='succeeded')
                    return ActionExecutionResult(ActionStatus.SUCCEEDED,observation=observation)
                worker=RunWorker(client.store,ReActKernel(LocalReadModel()),worker_id='resume-ui-fixture',action_executor=read_project,permission_mode='bypass-prompts')
                lease=await worker.claim(run_id)
                try:
                    await worker.execute(lease)
                finally:
                    await worker.release(lease)
                for todo in view.todos:
                    await client.coordinator._append(run_id,'TodoCompleted',{'todo_id':todo.todo_id},actor)
            await client.coordinator.cancel(run_id,'first turn ended')
        elif mode in ('followup','third'):
            receipt=await client.coordinator.send(run_id,'开发图书管理系统，前端 Vue3，后端 Spring Boot，数据库 MySQL' if mode=='followup' else '增加图书借阅与归还功能',follow_up=True)
            run_id=receipt.follow_up_run_id
            await LeaseManager(client.store).claim(run_id,'followup-task-verification')
            titles=('梳理前后端目录与依赖','实现图书管理接口与页面','运行验证并整理结果') if mode=='followup' else ('实现借阅与归还接口','补充借阅记录页面')
            for i,title in enumerate(titles):
                await client.coordinator._append(run_id,'TodoCreated',{'todo':{'id':f'step-{i}','title':title,'status':'running' if i==0 else 'pending','active_sessions':[run_id]}},actor)
        elif mode=='advance':
            await client.coordinator._append(run_id,'TodoCompleted',{'todo_id':'step-0'},actor)
            await client.coordinator._append(run_id,'TodoUpdated',{'todo':{'id':'step-1','title':'实现图书管理接口与页面','status':'in_progress','active_sessions':[run_id]}},actor)
        elif mode=='stop':
            await client.coordinator.cancel(run_id,'turn ended')
            await client.coordinator._append(run_id,'RunCancelled',{'reason':'local fixture has no worker; turn ended'},actor)
        print(json.dumps({'run_id':run_id}))
    finally:
        await client.close()
asyncio.run(main())`
  const result = await exec('python', ['-c', code, metadata.project_root, mode, id], { encoding: 'utf8', timeout: 20000 })
  return JSON.parse(result.stdout.trim().split('\n').at(-1))
}
const root = await change('create')
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto(metadata.base_url)
  await page.locator(`[data-run-id="${root.run_id}"]`).click()
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  const list = page.getByRole('dialog', { name: '任务列表', exact: true })
  await list.getByText('尚未生成执行计划，模型拆解任务后将在这里显示。', { exact: true }).waitFor()
  const child = await change('followup', root.run_id)
  await list.getByText('实现图书管理接口与页面', { exact: true }).waitFor()
  assert.equal(await list.locator('.task-list-row').count(), 3)
  assert.doesNotMatch(await list.innerText(), /你好/)
  await page.screenshot({ path: path.join(directory, '04-follow-up-task-list.png') })
  await change('advance', child.run_id)
  await list.getByText('1 / 3 已完成', { exact: true }).waitFor()
  await list.locator('.task-list-row[data-status="in_progress"]').waitFor()
  await page.screenshot({ path: path.join(directory, '05-follow-up-live-progress.png') })
  await change('stop', child.run_id)
  await change('third', root.run_id)
  await list.getByText('实现借阅与归还接口', { exact: true }).waitFor()
  assert.equal(await list.locator('.task-list-row').count(), 2)
  assert.doesNotMatch(await list.innerText(), /梳理前后端|实现图书管理接口/)
  await page.screenshot({ path: path.join(directory, '06-third-request-task-list.png') })
  await page.reload()
  await page.locator(`[data-run-id="${root.run_id}"]`).click()
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  await list.getByText('实现借阅与归还接口', { exact: true }).waitFor()
  const resumedRoot = await change('resume-create')
  await page.reload()
  await page.locator(`[data-run-id="${resumedRoot.run_id}"]`).click()
  await page.getByText('已读取项目说明，待继续实现页面。', { exact: true }).waitFor()
  await page.getByRole('button', { name: '继续', exact: true }).click()
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  await list.getByText('实现借阅页面', { exact: true }).waitFor()
  assert.equal(await list.locator('.task-list-row').count(), 3)
  await list.getByText('1 / 3 已完成', { exact: true }).waitFor()
  assert.doesNotMatch(await list.innerText(), /WebUI 用户继续/)
  await list.locator('.task-list-row.pending').nth(1).waitFor()
  assert.equal(await list.locator('.task-list-row.running').count(), 0)
  assert.equal(await list.locator('.task-list-row.pending').count(), 2)
  const state = await (await page.request.get(`${metadata.base_url}/api/sessions/${resumedRoot.run_id}`)).json()
  assert.equal(state.task_projection.goal.objective, '继续开发图书借阅功能')
  await page.getByRole('button', { name: '继续', exact: true }).waitFor({ state: 'hidden' })
  await page.screenshot({ path: path.join(directory, '07-resume-preserves-task-list.png') })
  assert.deepEqual(errors, [])
  await writeFile(path.join(directory, 'followup-task-verification.json'), JSON.stringify({ result: 'passed', source: 'Real durable follow-up runs and SSE', checks: ['greeting replaced by coding request while list stays open', 'child task statuses advance live', 'third request replaces previous tasks', 'reload restores latest tasks', 'Continue preserves objective and checklist with done count and pending states'], errors }, null, 2))
  console.log('PASS: subsequent requests replace task lists, update live, survive reload; Continue preserves the original checklist.')
} finally { await browser.close() }
