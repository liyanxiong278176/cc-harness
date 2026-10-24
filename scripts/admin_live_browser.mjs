/** Real browser controls and bounded evidence capture for the admin-system audit. */
import { createRequire } from 'node:module'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require('C:/Users/A2781/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright')
const dir = path.resolve('docs/design/screenshots/2026-10-24-admin-live')
await mkdir(dir, { recursive: true })
const { runId } = JSON.parse(await readFile(path.join(dir, 'target.json'), 'utf8'))
const base = process.env.ADMIN_AUDIT_BASE || 'http://127.0.0.1:3080'
const browser = await chromium.launch({ executablePath: 'C:/Users/A2781/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
page.setDefaultTimeout(20000)
const errors = [], timing = []
page.on('pageerror', e => errors.push(e.message))
page.on('requestfinished', r => { if (r.url().includes('/api/')) timing.push({ url: r.url(), ms: Math.round(r.timing().responseEnd) }) })
const mode = process.argv[2] || 'show'
try {
  if (mode === 'regression') {
    // Delay only the initial history response: use real data, preserve ordering.
    let release, intercepted
    const gate = new Promise(r => { release = r })
    const captured = new Promise(r => { intercepted = r })
    let once = false
    await page.route('**/api/sessions', async route => {
      if (route.request().method() !== 'GET' || once) return route.continue()
      once = true
      const response = await route.fetch()
      intercepted()
      await gate
      await route.fulfill({ response })
    })
    await page.goto(base)
    await captured
    await page.getByRole('button', { name: '新建会话', exact: true }).filter({ visible: true }).first().click()
    const input = page.getByRole('textbox', { name: '任务输入框' })
    await input.fill('草稿应保留在新会话中')
    release()
    await page.locator('.session-item').first().waitFor()
    await page.waitForTimeout(500)
    assert.equal(await page.locator('.session-item.active').count(), 0, 'late history must not select an old conversation')
    assert.equal(await page.locator('.session-loading').count(), 0)
    assert.equal(await input.inputValue(), '草稿应保留在新会话中')
    assert.equal(await input.isEnabled(), true)
    await page.screenshot({ path: path.join(dir, '03-bootstrap-race-after.png') })
    // Hold permission persistence; no messages may leave under stale settings.
    let releaseSave, saveCaptured
    const saving = new Promise(r => { saveCaptured = r })
    const saveGate = new Promise(r => { releaseSave = r })
    let sent = false, settingsPayload
    await page.route('**/api/settings', async route => {
      if (route.request().method() !== 'POST') return route.continue()
      settingsPayload = route.request().postDataJSON()
      saveCaptured()
      await saveGate
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ saved: true, settings: { model: 'deepseek-flash', permission_mode: settingsPayload.permission_mode, has_api_key: true } }) })
    })
    await page.route('**/api/messages', async route => {
      sent = true
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"audit intercepted; no task created"}' })
    })
    await page.locator('.permission-selector > button').click()
    const unchecked = page.getByRole('menuitemradio').filter({ hasNot: page.locator('.permission-option-check') })
    await unchecked.first().click()
    await saving
    assert.ok(settingsPayload.project_root?.includes('admin-system'), 'settings must bind the displayed project')
    assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true)
    await input.press('Enter')
    await page.waitForTimeout(300)
    assert.equal(sent, false)
    releaseSave()
    await page.getByRole('button', { name: '发送', exact: true }).waitFor()
    await page.waitForFunction(() => !document.querySelector('.send-button')?.disabled)
    await page.screenshot({ path: path.join(dir, '04-permission-save-after.png') })
    console.log('PASS: delayed history preserves blank selection and draft; delayed permission save blocks Enter/send and binds explicit project.')
  } else {
    await page.goto(base)
    await page.locator(`[data-run-id="${runId}"]`).waitFor()
    await page.locator(`[data-run-id="${runId}"]`).click()
    await page.locator('.session-loading').waitFor({ state: 'hidden' })
    if (mode === 'send') {
      const text = await readFile(process.argv[3], 'utf8')
      await page.getByRole('textbox', { name: '任务输入框' }).fill(text)
      const pending = page.waitForResponse(r => r.url().endsWith('/api/messages') && r.request().method() === 'POST')
      const start = Date.now()
      await page.getByRole('button', { name: '发送', exact: true }).click()
      const response = await pending
      console.log(JSON.stringify({ submittedMs: Date.now() - start, status: response.status(), receipt: await response.json() }))
      assert.equal(response.status(), 200)
    } else if (mode === 'edit') {
      const text = await readFile(process.argv[3], 'utf8')
      await page.getByRole('button', { name: '修改待执行消息', exact: true }).click()
      await page.getByRole('textbox', { name: '修改待执行消息', exact: true }).fill(text)
      const pending = page.waitForResponse(r => r.url().includes('/queue/') && r.request().method() === 'PATCH')
      await page.getByRole('button', { name: '保存', exact: true }).click()
      const response = await pending
      assert.equal(response.status(), 200)
      console.log(JSON.stringify({ editedQueue: true, receipt: await response.json() }))
    } else if (mode === 'pause' || mode === 'resume') {
      const pending = page.waitForResponse(r => r.url().endsWith('/' + mode) && r.request().method() === 'POST')
      const start = Date.now()
      await page.getByRole('button', { name: mode === 'pause' ? '暂停' : /^(确认)?继续$/, exact: true }).click()
      console.log(JSON.stringify({ action: mode, ms: Date.now() - start, receipt: await (await pending).json() }))
    } else if (mode === 'switch') {
      const input = page.getByRole('textbox', { name: '任务输入框' })
      await input.fill('未发送的开发验收草稿')
      const other = page.locator('.session-item').filter({ hasNot: page.locator(`[data-run-id="${runId}"]`) })
      const ids = await page.locator('.session-item').evaluateAll(xs => xs.map(x => x.getAttribute('data-run-id')))
      const otherId = ids.find(id => id && id !== runId)
      assert.ok(otherId)
      let start = Date.now()
      await page.locator(`[data-run-id="${otherId}"]`).click()
      await page.locator('.session-loading').waitFor({ state: 'hidden' })
      console.log(JSON.stringify({ action: 'switch-away', ms: Date.now() - start }))
      start = Date.now()
      await page.locator(`[data-run-id="${runId}"]`).click()
      await page.locator('.session-loading').waitFor({ state: 'hidden' })
      assert.equal(await input.inputValue(), '未发送的开发验收草稿')
      console.log(JSON.stringify({ action: 'switch-back', ms: Date.now() - start, draftPreserved: true }))
    } else if (mode === 'observe') {
      await page.getByRole('button', { name: '任务列表', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: '任务列表', exact: true })
      const samples = []
      for (let i = 0; i < 12; i++) {
        samples.push({ at: new Date().toISOString(), text: await dialog.innerText() })
        await page.waitForTimeout(5000)
      }
      await writeFile(path.join(dir, 'live-task-samples-' + Date.now() + '.json'), JSON.stringify(samples, null, 2))
      console.log(JSON.stringify(samples.filter((x, i) => !i || x.text !== samples[i - 1].text)))
    }
    const state = await (await page.request.get(base + '/api/sessions/' + runId)).json()
    await writeFile(path.join(dir, 'latest-state.json'), JSON.stringify(state, null, 2))
    console.log(JSON.stringify({ status: state.status, seq: state.sequence, taskRunId: state.task_run_id, reason: state.task?.reason, todos: state.task_projection?.todos?.map(t => ({ title: t.title, status: t.status })) }))
    console.log((await page.locator('body').innerText()).slice(-1800))
    await page.screenshot({ path: path.join(dir, mode + '-' + Date.now() + '.png') })
  }
  assert.deepEqual(errors, [])
  await writeFile(path.join(dir, mode + '-timing-' + Date.now() + '.json'), JSON.stringify({ errors, timing }, null, 2))
} finally {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await browser.close()
}
