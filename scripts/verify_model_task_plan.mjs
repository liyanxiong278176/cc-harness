import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { createInterface } from 'node:readline'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const metadata = JSON.parse(await readFile('docs/design/reference/redesign-preview.json', 'utf8'))
const child = spawn('python', ['-u', '-m', 'scripts.preview_model_plan_worker', metadata.project_root])
let errors = ''
child.stderr.on('data', chunk => { errors += chunk })
const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]()
const first = await lines.next()
assert.ok(!first.done, errors)
const fixture = JSON.parse(first.value)
const completed = new Promise((resolve, reject) => child.on('exit', code => code === 0 ? resolve() : reject(new Error(errors))))
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const pageErrors = []
page.on('pageerror', error => pageErrors.push(error.message))
const directory = path.resolve('docs/design/screenshots/2026-10-24')
await mkdir(directory, { recursive: true })
try {
  await page.goto(metadata.base_url)
  await page.locator(`[data-run-id="${fixture.run_id}"]`).click()
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  const list = page.getByRole('dialog', { name: '任务列表', exact: true })
  await list.getByText('尚未生成执行计划，模型拆解任务后将在这里显示。', { exact: true }).waitFor()
  assert.equal(await list.locator('.task-list-row').count(), 0)
  await page.screenshot({ path: path.join(directory, '09-waiting-for-model-plan.png') })
  await writeFile(fixture.start, 'start')
  await list.getByText('检查项目结构与现有接口', { exact: true }).waitFor()
  await list.getByText('验证接口、页面与数据库连接', { exact: true }).waitFor()
  assert.equal(await list.locator('.task-list-row').count(), 3)
  assert.doesNotMatch(await list.innerText(), /开发图书管理系统，使用/)
  await page.screenshot({ path: path.join(directory, '10-model-generated-task-plan.png') })
  await writeFile(fixture.advance, 'advance')
  await completed
  await list.getByText('1 / 3 已完成', { exact: true }).waitFor()
  const active = list.locator('.task-list-row.running')
  assert.match(await active.innerText(), /实现图书接口与管理页面/)
  await page.screenshot({ path: path.join(directory, '11-model-plan-live-progress.png') })
  await page.reload()
  await page.locator(`[data-run-id="${fixture.run_id}"]`).click()
  await page.getByRole('button', { name: '任务列表', exact: true }).click()
  await list.getByText('1 / 3 已完成', { exact: true }).waitFor()
  assert.deepEqual(pageErrors, [])
  await writeFile(path.join(directory, 'model-plan-verification.json'), JSON.stringify({ result: 'passed', source: 'Deterministic model tool calls executed through production Worker and task_plan; real README read, no remote model calls or direct Todo fixture writes', checks: ['bootstrap request hidden before planning', 'model-authored step titles persisted and streamed', 'model updates completion and current step after real read', 'reload restores step plan'], pageErrors }, null, 2))
  console.log('PASS: model -> task_plan -> durable checklist -> SSE -> reload.')
} finally {
  child.kill()
  await browser.close()
}
