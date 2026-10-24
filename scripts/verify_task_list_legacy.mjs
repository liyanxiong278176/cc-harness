/** Read-only browser verification against an already persisted legacy chat. */
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const runId = process.argv[3]
if (!runId) throw new Error('Pass an existing legacy root Run ID; this test never creates or resumes work.')
const base = process.argv[4] || 'http://127.0.0.1:3080'
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  const timeline = await (await page.request.get(`${base}/api/sessions/${runId}/timeline?limit=10000`)).json()
  const request = timeline.events.find(event => event.event_type === 'FollowUpQueued'
    && event.payload?.continuation === false && event.content?.includes('图书管理系统'))
  assert.ok(request, 'The existing history must include the actual library-system request.')
  const directory = path.resolve('docs/design/screenshots/2026-10-24')
  await mkdir(directory, { recursive: true })
  await page.goto(base)
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt) await page.reload()
    await page.locator(`[data-run-id="${runId}"]`).click()
    await page.getByRole('button', { name: '任务列表', exact: true }).click()
    const list = page.getByRole('dialog', { name: '任务列表', exact: true })
    try {
      await list.getByText('尚未生成执行计划，模型拆解任务后将在这里显示。', { exact: true }).waitFor()
    } catch (error) {
      console.log('Task dialog:', await list.innerText())
      await page.screenshot({ path: path.join(directory, 'legacy-task-debug.png') })
      throw error
    }
    assert.doesNotMatch(await list.innerText(), /WebUI 用户继续|Continuation request/)
    assert.equal(await list.locator('.task-list-row').count(), 0)
    assert.ok(!(await list.innerText()).includes(request.content))
    assert.equal(await page.locator('.plan-checklist').count(), 0)
    if (attempt) await page.screenshot({ path: path.join(directory, '12-existing-session-no-fabricated-plan.png') })
  }
  assert.deepEqual(errors, [])
  await writeFile(path.join(directory, 'legacy-task-verification.json'), JSON.stringify({ result: 'passed', source: 'Existing persisted conversation on localhost:3080; GET requests and UI navigation only', checks: ['old bootstrap task and raw user prompt are hidden', 'absence of model plan is explicit', 'reload preserves truthful empty state without new requests or resume', 'no page errors'], errors }, null, 2))
  console.log('PASS: existing session without a model plan shows an explicit empty state, never the user prompt as a planned step.')
} finally { await browser.close() }
