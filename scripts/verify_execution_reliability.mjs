/** Read-only browser evidence for the repaired existing conversation. */
import { createRequire } from 'node:module'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const target = JSON.parse(await readFile('docs/design/reference/resume-upgrade-target.json', 'utf8'))
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const directory = path.resolve('docs/design/screenshots/2026-10-24')
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto('http://localhost:3080')
  await page.locator(`[data-run-id="${target.run_id}"]`).click()
  await page.locator('.composer-footer').waitFor()
  const latest = page.getByRole('button', { name: '跳到最新消息' })
  if (await latest.isVisible()) await latest.click()
  const state = await (await page.request.get(`http://localhost:3080/api/sessions/${target.run_id}`)).json()
  const projection = state.task_projection
  assert.equal((projection?.actions || []).filter(action => action.status === 'outcome_unknown').length, 0)
  assert.ok(['running', 'queued', 'completed', 'stalled', 'failed_recoverable'].includes(state.status), state.task?.reason || state.status)
  assert.equal(state.task_run_id, target.task_run_id)
  await mkdir(directory, { recursive: true })
  await page.screenshot({ path: path.join(directory, '17-runtime-reliability-recovered.png') })
  const list = page.getByRole('button', { name: /任务列表/ }).first()
  if (await list.isVisible()) {
    await list.click()
    await page.screenshot({ path: path.join(directory, '18-runtime-task-list.png') })
  }
  assert.deepEqual(errors, [])
  await writeFile(path.join(directory, 'execution-reliability-verification.json'), JSON.stringify({ result: 'passed', status: state.status, task_run_id: state.task_run_id, unknown_actions: 0, browser_errors: errors, mutations: 'none; browser inspection only' }, null, 2))
  console.log(JSON.stringify({ result: 'passed', status: state.status, browserErrors: errors.length }))
} finally { await browser.close() }
