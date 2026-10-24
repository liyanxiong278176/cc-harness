/** Browser verification against the isolated production Runtime fixture.
 * Start python -m scripts.preview_webui_redesign, then:
 * node scripts/verify_webui_redesign.mjs [path-to-playwright-package]
 */
import { createRequire } from 'node:module'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.argv[2] || 'playwright')
const metadata = JSON.parse(await readFile('docs/design/reference/redesign-preview.json', 'utf8'))
const directory = path.resolve('docs/design/screenshots/2026-10-23')
await mkdir(directory, { recursive: true })
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1600, height: 1050 }, deviceScaleFactor: 1 })
const errors = [], checks = [], shots = []
page.on('pageerror', error => errors.push(error.message))
const wait = () => page.waitForTimeout(650)
async function shot(filename, title) {
  await wait()
  await page.locator('.toast').waitFor({ state: 'hidden' })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `horizontal overflow: ${filename}`)
  await page.screenshot({ path: path.join(directory, filename), fullPage: true })
  shots.push({ filename, title })
  console.log(`Screenshot: ${filename}`)
}
async function session(text) {
  await page.locator('.session-item').filter({ hasText: text }).click()
  await page.waitForSelector('.session-loading', { state: 'hidden' })
  await wait()
}
try {
  await page.goto(metadata.base_url)
  await page.waitForSelector('.app-shell')
  await page.locator('.app-navigation-item').filter({ hasText: '新建会话' }).click()
  await shot('01-welcome-dark.png', '新会话 · 深色')
  await page.getByRole('button', { name: '代码审查', exact: false }).click()
  assert.match(await page.getByRole('textbox', { name: '任务输入框' }).inputValue(), /审查/)
  checks.push('Welcome suggestion populates the real composer')

  await session('检查项目结构，梳理任务筛选')
  assert.ok(await page.locator('.turn-process').getAttribute('open') !== null)
  assert.equal(await page.locator('.tool-activity[open]').count(), 0)
  await shot('02-running.png', '执行中 · 进展与计划')
  checks.push('Running process visible; raw tool output stays collapsed')

  await session('为任务列表增加状态筛选')
  await page.getByText('4 项检查全部通过', { exact: false }).waitFor()
  assert.equal(await page.locator('.turn-process[open]').count(), 0)
  await shot('03-completed.png', '完成 · 回复与文件摘要')
  checks.push('Completed process collapses; final answer remains visible')

  await page.getByRole('tab', { name: /轨迹/ }).click()
  await page.getByRole('button', { name: '工具调用', exact: true }).click()
  assert.equal(await page.locator('.trace-row').count(), 3)
  await page.getByRole('textbox', { name: '搜索执行轨迹' }).fill('src/tasks.py')
  assert.equal(await page.locator('.trace-row').count(), 2)
  await page.getByRole('textbox', { name: '搜索执行轨迹' }).fill('')
  await page.locator('.trace-row').filter({ hasText: 'run_command' }).locator('summary').first().click()
  await shot('04-trace.png', '轨迹 · 工具结果与搜索')
  checks.push('Trace filtering, search, expansion and actual test output')

  await page.getByRole('tab', { name: '对话', exact: true }).click()
  await page.locator('.changes-summary > button').click()
  await page.locator('.diff-code').waitFor()
  assert.match(await page.locator('.diff-code').innerText(), /status == "completed"/)
  await shot('05-diff-dark.png', '任务变更 · 真实文本差异')
  checks.push('Task-scoped diff loads immutable native mutation evidence')
  await page.getByRole('button', { name: '收起右侧面板', exact: true }).click()

  await session('检查发布脚本')
  await page.getByRole('region', { name: '待处理审批' }).waitFor()
  await page.locator('.approval-arguments summary').click()
  await shot('06-approval-queue.png', '审批 · 操作范围与待执行消息')
  await page.getByRole('button', { name: '修改待执行消息', exact: true }).click()
  await page.getByRole('textbox', { name: '修改待执行消息', exact: true }).fill('检查结束后，只整理变更摘要。')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await page.locator('.editable-queue').getByText('检查结束后，只整理变更摘要。', { exact: true }).waitFor()
  const queued = await (await page.request.get(`${metadata.base_url}/api/sessions/${metadata.approval}/timeline`)).json()
  assert.ok(queued.events.some(event => event.event_type === 'FollowUpUpdated' && event.content === '检查结束后，只整理变更摘要。'))
  await shot('07-queue-updated.png', '队列修改 · 已持久化')
  await page.getByRole('button', { name: '撤回待执行消息', exact: true }).click()
  await page.locator('.editable-queue').waitFor({ state: 'hidden' })
  checks.push('Queue edits persist in Runtime; withdrawal removes pending work')

  await page.locator('.app-navigation-item').filter({ hasText: '模型与设置' }).click()
  await page.getByRole('dialog').waitFor()
  await shot('08-settings.png', '模型与权限设置')
  await page.keyboard.press('Escape')
  checks.push('Settings dialog opens and Escape restores workspace')

  await page.getByRole('button', { name: '本地能力与记忆', exact: true }).click()
  await page.getByRole('dialog').waitFor()
  await shot('15-memory-capabilities.png', '本地能力与记忆')
  await page.keyboard.press('Escape')
  await page.locator('.permission-trigger').click()
  await page.getByRole('menu', { name: '权限策略' }).waitFor()
  await shot('16-permission-policy.png', '权限策略 · 真实生效范围')
  await page.keyboard.press('Escape')

  await page.getByRole('button', { name: '运行面板', exact: true }).click()
  await shot('09-runtime-details.png', '任务详情 · 上下文与能力')
  await page.getByRole('button', { name: '收起右侧面板', exact: true }).click()

  await session('运行项目检查，汇报失败原因')
  await shot('10-recoverable-error.png', '可恢复失败 · 对话内处理')

  await page.getByRole('button', { name: '切换浅色主题', exact: true }).click()
  await page.locator('.app-navigation-item').filter({ hasText: '新建会话' }).click()
  await shot('11-welcome-light.png', '新会话 · 浅色')
  await session('为任务列表增加状态筛选')
  await page.locator('.changes-summary > button').click()
  await page.locator('.diff-code').waitFor()
  await shot('12-diff-light.png', '文件差异 · 浅色')
  await page.getByRole('button', { name: '当前文件', exact: false }).click()
  await page.locator('.artifact-code-view').waitFor()
  await shot('13-file-preview.png', '当前文件 · 代码预览')
  checks.push('Current file preview remains separate from historical task diff')
  await page.getByRole('button', { name: '收起右侧面板', exact: true }).click()

  await page.getByRole('button', { name: '切换深色主题', exact: true }).click()
  await page.locator('.app-navigation-item').filter({ hasText: '新建会话' }).click()
  await page.setViewportSize({ width: 430, height: 932 })
  await page.getByRole('button', { name: '收起侧栏', exact: true }).first().click()
  await shot('14-mobile.png', '窄屏 · 新会话')
  checks.push('Narrow viewport has no page overflow')

  await page.setViewportSize({ width: 1600, height: 1050 })
  await page.getByRole('button', { name: '展开侧栏', exact: true }).click()
  await session('检查项目结构，梳理任务筛选')
  await page.getByRole('button', { name: '停止运行', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('.stop-button'), null, { timeout: 12000 })
  const stopped = await (await page.request.get(`${metadata.base_url}/api/sessions/${metadata.active}`)).json()
  assert.equal(stopped.status, 'cancelled')
  checks.push('Stop reaches a durable cancelled boundary for the held real worker')

  await page.reload()
  await page.waitForSelector('.app-shell')
  await session('为任务列表增加状态筛选')
  await page.getByText('4 项检查全部通过', { exact: false }).waitFor()
  checks.push('Reload restores durable transcript and final answer')
  assert.deepEqual(errors, [])
  await writeFile(path.join(directory, 'verification.json'), JSON.stringify({ data: metadata.data_kind, checks, errors, screenshots: shots }, null, 2))
  const ordered = [...shots].sort((a, b) => a.filename.localeCompare(b.filename))
  await writeFile(path.join(directory, 'index.html'), `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>cc-harness 重设计截图册</title><style>body{margin:0;background:#17191c;color:#e9edf2;font:14px/1.8 system-ui,'Microsoft YaHei',sans-serif}header,main{max-width:1400px;margin:auto;padding:30px}h1{font-size:26px;font-weight:550}p{color:#929ba8}nav{display:flex;flex-wrap:wrap;gap:8px}a{color:#8ab2f5;text-decoration:none}nav a{padding:5px 10px;border:1px solid #ffffff15;border-radius:5px}section{margin:45px 0 70px;scroll-margin-top:20px}h2{font-size:16px;font-weight:500}img{display:block;width:100%;height:auto;border:1px solid #ffffff15;border-radius:8px}section:has(img[src*="mobile"]) img{max-width:430px;margin:auto}</style><header><h1>cc-harness · WebUI 重设计</h1><p>真实浏览器截图 · 隔离本地验证项目。模型回复由确定性适配器提供，文件修改、测试、审批、队列与事件均通过生产 Runtime。点击目录逐张查看，点击图片查看原图。</p><nav>${ordered.map((item, index) => `<a href="#shot-${index}">${index + 1}. ${item.title}</a>`).join('')}</nav></header><main>${ordered.map((item, index) => `<section id="shot-${index}"><h2>${index + 1}. ${item.title}</h2><a href="${item.filename}"><img loading="lazy" src="${item.filename}" alt="${item.title}"></a></section>`).join('')}</main></html>`)
  console.log(JSON.stringify({ passed: checks.length, screenshots: shots.length, errors }))
} finally {
  await browser.close()
}
