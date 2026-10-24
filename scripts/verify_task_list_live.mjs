/** Verify task-list changes through real durable events and the existing SSE connection. */
import { createRequire } from 'node:module'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const exec = promisify(execFile)
const metadata = JSON.parse(await readFile('docs/design/reference/redesign-preview.json', 'utf8'))
const directory = path.resolve('docs/design/screenshots/2026-10-24')
await mkdir(directory, { recursive: true })

async function events(mode, runId = '') {
  const code = `import asyncio,json,sys
from pathlib import Path
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.run_events import EventActor
from cc_harness.lease import LeaseManager
async def main():
    project=Path(sys.argv[1])
    client=await DurableRuntimeClient.create(project,data_root=project.parent/'runtime')
    try:
        mode,run_id=sys.argv[2],sys.argv[3]
        if mode=='create':
            run_id=await client.submit('验证输入框上方的任务列表实时更新',('任务列表随真实事件更新',))
            await LeaseManager(client.store).claim(run_id,'task-list-verification')
            for i,title in enumerate(('读取项目说明与入口文件','检查任务筛选的实现','运行筛选功能验证')):
                await client.coordinator._append(run_id,'TodoCreated',{'todo':{'id':f'live-{i}','title':title,'status':'running' if i==0 else 'pending','active_sessions':[run_id]}},EventActor('client','task-list-verification'))
        else:
            (project/'README.md').read_text(encoding='utf-8')
            changes=[('live-0','done'),('live-1','in_progress')] if mode=='advance' else [('live-1','done'),('live-2','failed')]
            for todo_id,status in changes:
                kind='TodoCompleted' if status=='done' else 'TodoUpdated'
                payload={'todo_id':todo_id} if status=='done' else {'todo':{'id':todo_id,'title':'运行筛选功能验证' if todo_id=='live-2' else '检查任务筛选的实现','status':status,'active_sessions':[run_id]}}
                await client.coordinator._append(run_id,kind,payload,EventActor('client','task-list-verification'))
        print(json.dumps({'run_id':run_id}))
    finally:
        await client.close()
asyncio.run(main())`
  const result = await exec('python', ['-c', code, metadata.project_root, mode, runId], { encoding: 'utf8', timeout: 20000 })
  return JSON.parse(result.stdout.trim().split('\n').at(-1))
}

const run = await events('create')
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 })
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto(metadata.base_url)
  await page.locator('.session-item').filter({ hasText: '验证输入框上方的任务列表实时更新' }).first().click()
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  const list = page.getByRole('dialog', { name: '任务列表', exact: true })
  await list.waitFor()
  await list.locator('.task-list-row').nth(2).waitFor()
  assert.equal(await list.locator('.task-list-row').count(), 3)
  assert.equal(await list.locator('.task-list-row.running').count(), 1)
  assert.equal(await list.locator('.task-list-row.pending').count(), 2)
  assert.doesNotMatch(await list.innerText(), /目标契约|PlanGraph/)
  await page.screenshot({ path: path.join(directory, '01-task-list-running.png') })

  // No reload, UI route changes, or list re-opening between these updates.
  await events('advance', run.run_id)
  await list.locator('.task-list-row.done').waitFor()
  await list.getByText('1 / 3 已完成', { exact: true }).waitFor()
  await list.locator('.task-list-row[data-status="in_progress"]').waitFor()
  await page.screenshot({ path: path.join(directory, '02-task-list-live-update.png') })

  await events('failure', run.run_id)
  await list.getByText('2 / 3 已完成', { exact: true }).waitFor()
  await list.locator('.task-list-row.failed').waitFor()
  await page.screenshot({ path: path.join(directory, '03-task-list-failed.png') })
  await page.keyboard.press('Escape')
  await list.waitFor({ state: 'hidden' })
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  await list.getByText('2 / 3 已完成', { exact: true }).waitFor()
  assert.deepEqual(errors, [])
  await writeFile(path.join(directory, 'task-list-verification.json'), JSON.stringify({ result: 'passed', source: 'Real durable Todo events delivered over SSE; no reload or reopened list during updates', states: ['0/3 running', '1/3 running', '2/3 failed'], errors }, null, 2))
  console.log('PASS: task list updates over SSE while open; status, count, failure, and Escape verified.')
} finally {
  await browser.close()
}
