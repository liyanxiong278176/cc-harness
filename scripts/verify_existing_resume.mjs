/** Resume the user's already-authorized task once; never submit a chat message. */
import { createRequire } from 'node:module'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const target = JSON.parse(await readFile('docs/design/reference/resume-upgrade-target.json', 'utf8'))
const base = 'http://127.0.0.1:3080'
const observeOnly = process.argv.includes('--observe')
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const posts = []
page.on('request', request => { if (request.method() === 'POST') posts.push(request.url()) })
try {
  await page.goto(base)
  await page.locator(`[data-run-id="${target.run_id}"]`).click()
  if (!observeOnly) {
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/resume') && response.status() === 200, { timeout: 60000 })
    await page.getByRole('button', { name: /^(确认)?继续$/ }).click()
    const receipt = await (await responsePromise).json()
    console.log(JSON.stringify({ resumeAccepted: receipt.resumed, status: receipt.status, task_run_id: receipt.task_run_id }))
  }
  assert.equal(posts.filter(url => url.endsWith('/api/messages')).length, 0)
  await page.waitForFunction(async ({ base, id }) => {
    const state = await (await fetch(`${base}/api/sessions/${id}`)).json()
    return ['running', 'completed'].includes(state.status)
  }, { base, id: target.run_id }, { timeout: 150000, polling: 2000 })
  const state = await (await page.request.get(`${base}/api/sessions/${target.run_id}`)).json()
  assert.ok(['running', 'queued', 'completed'].includes(state.status), state.task?.reason || state.status)
  const timeline = await (await page.request.get(`${base}/api/sessions/${target.run_id}/timeline?limit=10000`)).json()
  const taskEvents = timeline.events.filter(event => event.run_id === target.task_run_id)
  const resumedAt = taskEvents.findLastIndex(event => event.event_type === 'RunResumed')
  assert.ok(taskEvents.slice(resumedAt + 1).some(event => ['RunClaimed', 'ModelInvocationStarted'].includes(event.event_type)), 'The scheduler must actually claim work after the latest resume')
  await page.locator('.composer-footer').getByRole('button', { name: '暂停', exact: true }).waitFor()
  assert.doesNotMatch(await page.locator('body').innerText(), /WebUI 用户继续|webui\.resume/)
  const directory = path.resolve('docs/design/screenshots/2026-10-24')
  await mkdir(directory, { recursive: true })
  await page.screenshot({ path: path.join(directory, '15-existing-task-resumed-without-message.png') })
  await writeFile(path.join(directory, 'existing-resume-verification.json'), JSON.stringify({ result: 'passed', mode: observeOnly ? 'observe prior accepted resume without issuing another control' : 'click Continue once', status: state.status, checks: ['no message submission', 'execution running after scheduler recovery', 'no system resume bubbles'] }, null, 2))
  console.log('PASS: existing task running after one Continue click, no new chat message.')
} finally { await browser.close() }
