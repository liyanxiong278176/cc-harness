import { createRequire } from 'node:module'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import assert from 'node:assert/strict'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const metadata = JSON.parse(await readFile('docs/design/reference/resume-preview.json', 'utf8'))
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const directory = path.resolve('docs/design/screenshots/2026-10-24')
await mkdir(directory, { recursive: true })
const requests = []
const errors = []
page.on('request', request => { if (request.method() === 'POST') requests.push(request.url()) })
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto(metadata.base_url)
  await page.locator(`[data-run-id="${metadata.run_id}"]`).click()
  const button = page.getByRole('button', { name: '继续', exact: true })
  await button.waitFor()
  // Keep the response pending to verify immediate feedback and duplicate-click protection.
  await page.route('**/api/sessions/*/resume', async route => {
    await new Promise(resolve => setTimeout(resolve, 1200))
    await route.continue()
  })
  await button.click()
  const pending = page.getByRole('button', { name: '正在恢复…', exact: true })
  await pending.waitFor()
  assert.equal(await pending.isDisabled(), true)
  await page.screenshot({ path: path.join(directory, '13-continue-immediate-feedback.png') })
  await page.waitForResponse(response => response.url().endsWith('/resume') && response.status() === 200)
  await page.waitForFunction(async ({ base, id }) => {
    const response = await fetch(`${base}/api/sessions/${id}/timeline?limit=1000`)
    const timeline = await response.json()
    return timeline.events.some(event => event.event_type === 'ToolObservationCommitted')
  }, { base: metadata.base_url, id: metadata.run_id })
  assert.equal(requests.filter(url => url.endsWith('/resume')).length, 1)
  assert.equal(requests.filter(url => url.endsWith('/api/messages')).length, 0)
  const state = await (await page.request.get(`${metadata.base_url}/api/sessions/${metadata.run_id}`)).json()
  assert.equal(state.status, 'running')
  await page.locator('.composer-footer').getByRole('button', { name: '暂停', exact: true }).waitFor()
  assert.doesNotMatch(await page.locator('.conversation').count() ? await page.locator('.conversation').innerText() : await page.locator('main').innerText(), /WebUI 用户继续|webui\.resume/)
  await page.screenshot({ path: path.join(directory, '14-continue-runs-without-user-message.png') })
  await page.reload()
  await page.locator(`[data-run-id="${metadata.run_id}"]`).click()
  await page.locator('.composer-footer').getByRole('button', { name: '暂停', exact: true }).waitFor()
  assert.doesNotMatch(await page.locator('body').innerText(), /WebUI 用户继续|webui\.resume/)
  await writeFile(metadata.gate, 'release')
  assert.deepEqual(errors, [])
  await writeFile(path.join(directory, 'resume-control-verification.json'), JSON.stringify({ result: 'passed', source: 'Real local supervisor claims paused run and executes Read after UI Continue; no remote model', checks: ['immediate disabled spinner', 'one resume POST and zero message POSTs', 'real tool observation committed', 'running state and pause control visible', 'no control-message bubbles before or after reload'], errors }, null, 2))
  console.log('PASS: Continue starts real execution, shows pending feedback, and adds no user message.')
} finally { await browser.close() }
