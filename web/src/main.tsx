import { StrictMode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import type { LucideIcon } from 'lucide-react'
import {
  AlertCircle, Bot, Check, ChevronDown, ChevronRight,
  CircleAlert, CircleCheck, CircleStop, Command, Copy, Eye, EyeOff,
  FileCode2, Folder, FolderOpen, History, Info, Keyboard, LoaderCircle,
  MessageSquarePlus, Moon, MoreHorizontal, PanelLeftClose, PanelLeftOpen,
  PanelRightClose, PanelRightOpen, Play, RefreshCw, RotateCcw, Search,
  Send, Settings, ShieldCheck, Sparkles, Sun, X,
} from 'lucide-react'
import './styles.css'

type SettingsState = {
  base_url: string
  model: string
  permission_mode: PermissionMode
  has_api_key: boolean
  api_key_masked: string
  api_key?: string
}
type PermissionMode = 'default' | 'auto-edit' | 'bypass-prompts'
type PermissionModeSpec = {
  mode: PermissionMode
  label: string
  description: string
}
type Session = {
  run_id: string
  title: string
  status: string
  sequence: number
  active_worker_id?: string | null
  project_root?: string
}
type EventItem = {
  id: string
  event_id: string
  run_id: string
  sequence: number
  event_type: string
  occurred_at: string
  kind: 'user' | 'assistant' | 'tool' | 'error' | 'outcome' | 'status'
  content?: string
  tool_name?: string | null
  status?: string
  payload?: Record<string, unknown>
  outcome?: Record<string, unknown>
  optimistic?: boolean
  /** Public event id returned with a command receipt, used for reconciliation. */
  ack_event_id?: string
}
type ConversationTurn = {
  id: string
  user?: EventItem
  process: EventItem[]
  assistant?: EventItem
}
type ContextState = {
  used_tokens: number | null
  window_tokens: number | null
  ratio: number | null
  effective_window_tokens?: number | null
  preflight_ratio?: number | null
  source: string
  categories: Record<string, number> | null
  compaction?: {
    tier?: string
    before_tokens?: number
    after_tokens?: number
    ratio_before?: number
    ratio_after?: number
    summarized?: boolean
    error?: string | null
    effective_context_window?: number | null
    provider_safety_factor?: number | null
    applied?: boolean
  } | null
  capabilities?: {
    context?: { initialized?: boolean; triggered?: boolean; degraded_reason?: string | null; details?: Record<string, unknown> }
    memory?: { enabled?: boolean; initialized?: boolean; triggered?: boolean; degraded_reason?: string | null; details?: Record<string, unknown> }
    safety?: { enabled?: boolean; initialized?: boolean; triggered?: boolean; degraded_reason?: string | null; details?: Record<string, unknown> }
  }
  note?: string
}
type ExecutorState = {
  requested_backend?: string | null
  backend?: string | null
  sandbox_available?: boolean | null
  degraded?: boolean
  fallback_reason?: string | null
}
type Bootstrap = {
  project: { root: string } | null
  settings: SettingsState
  sessions: Session[]
  initial_prompt?: string | null
}
type Theme = 'dark' | 'light'
type CommandItem = {
  id: string
  command: string
  label: string
  description: string
  icon: LucideIcon
}

const permissionModeSpecs: PermissionModeSpec[] = [
  { mode: 'default', label: '请求批准', description: '编辑外部文件和使用互联网时始终询问' },
  { mode: 'auto-edit', label: '帮我批准', description: '仅对检测到的风险操作请求批准' },
  { mode: 'bypass-prompts', label: '完全访问权限', description: '可不受限制地访问互联网和你电脑上的任何文件' },
]

function normalizePermissionMode(value: string | undefined): PermissionMode {
  return permissionModeSpecs.some((item) => item.mode === value)
    ? value as PermissionMode
    : 'default'
}

const statusLabels: Record<string, string> = {
  draft: '草稿',
  queued: '排队中',
  running: '运行中',
  awaiting_approval: '等待审批',
  waiting_on_predecessor: '等待前置任务',
  stalled: '已暂停',
  blocked: '已阻塞',
  cancel_requested: '正在停止',
  cancelled: '已停止',
  failed_recoverable: '可恢复失败',
  failed_terminal: '失败',
  completed: '已完成',
}

const commandItems: CommandItem[] = [
  { id: 'new', command: '/new', label: '新建会话', description: '在当前项目创建一个新的 Durable Run', icon: MessageSquarePlus },
  { id: 'project', command: '/project', label: '选择项目', description: '切换本机项目文件夹', icon: FolderOpen },
  { id: 'status', command: '/status', label: '刷新状态', description: '读取当前会话的 Runtime 状态', icon: RefreshCw },
  { id: 'resume', command: '/resume', label: '继续运行', description: '从检查点继续，或确认解除目标审核阻塞', icon: RotateCcw },
  { id: 'settings', command: '/settings', label: '打开设置', description: '配置 Base URL、API Key 和模型', icon: Settings },
  { id: 'clear', command: '/clear', label: '清空视图', description: '清空当前浏览器视图，不删除审计事件', icon: X },
  { id: 'help', command: '/help', label: '帮助', description: '查看 WebUI 快捷命令', icon: Command },
]

const emptyContext: ContextState = {
  used_tokens: null,
  window_tokens: null,
  ratio: null,
  effective_window_tokens: null,
  preflight_ratio: null,
  source: 'unavailable',
  categories: null,
  compaction: null,
  capabilities: {},
}
const emptyExecutor: ExecutorState = {
  requested_backend: null,
  backend: null,
  sandbox_available: null,
  degraded: false,
  fallback_reason: null,
}

const runtimeEventLabels: Record<string, string> = {
  RunCreated: '任务已创建',
  GoalContractAccepted: '目标已确认',
  PlanDiscoveryStarted: '正在拆解任务',
  PlanDiscoveryCompleted: '任务拆解完成',
  PlanNodeStarted: '开始执行步骤',
  PlanNodeCompleted: '步骤已完成',
  RunClaimed: 'Worker 已接管',
  WorkerHeartbeat: 'Worker 运行中',
  RunSegmentStarted: '开始新一轮执行',
  RunSegmentFinished: '本轮执行完成',
  ActionPlanned: '已规划工具动作',
  ActionStarted: '工具动作执行中',
  ActionSucceeded: '工具动作完成',
  ActionFailed: '工具动作失败',
  ActionOutcomeUnknown: '工具结果待确认',
  ContextProjectionBuilt: '上下文已更新',
  ContextCompacted: '上下文已压缩',
  ApprovalRequested: '等待审批',
  RunResumed: '已继续运行',
  RunYielded: '运行暂存',
  RunOutcomeRecorded: '运行结果已记录',
  RunStalled: '运行已暂停',
  RunCancelled: '运行已停止',
  ModelInvocationStarted: '模型调用中',
  ModelInvocationFinished: '模型调用完成',
  ModelInvocationFailed: '模型调用失败',
  TodoUpdated: '任务清单已更新',
  StallDiagnosisRecorded: '已记录暂停诊断',
}

const runtimeStatusLabels: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  started: '执行中',
  succeeded: '执行成功',
  completed: '已完成',
  failed: '执行失败',
  cancelled: '已停止',
  unknown: '结果待确认',
}

function projectDisplayName(root: string) {
  return root.split(/[\\/]/).filter(Boolean).pop() || root
}

function runtimeEventLabel(event: EventItem) {
  const eventLabel = runtimeEventLabels[event.event_type]
  if (eventLabel) return eventLabel
  if (event.payload?.status) {
    const value = String(event.payload.status)
    return statusLabels[value] ?? runtimeStatusLabels[value] ?? value
  }
  return event.event_type
}

function eventTime(event: EventItem) {
  const value = Date.parse(event.occurred_at)
  return Number.isFinite(value) ? value : 0
}

function normalizedUserContent(value: string | undefined) {
  return (value ?? '').replace(/\r\n/g, '\n').trim()
}

/**
 * Convert the durable event stream into the compact turn shape used by the
 * DeepSeek Harness chat. A turn keeps the user's request and the final
 * assistant answer visible; intermediate assistant updates and tool calls are
 * retained as an expandable process section instead of flooding the thread.
 */
function groupConversationTurns(events: EventItem[]): ConversationTurn[] {
  const turns: ConversationTurn[] = []
  let current: ConversationTurn | null = null
  for (const event of events) {
    if (event.kind === 'user') {
      if (current) turns.push(current)
      current = { id: event.id, user: event, process: [] }
      continue
    }
    if (!current) {
      // A reconnect can begin in the middle of a run. Keep those process
      // events visible, but do not manufacture a user message for them.
      current = { id: 'orphan-' + event.id, process: [] }
    }
    if (event.kind === 'assistant' && normalizedUserContent(event.content)) {
      if (current.assistant) current.process.push(current.assistant)
      current.assistant = event
    } else {
      current.process.push(event)
    }
  }
  if (current) turns.push(current)
  return turns
}

/**
 * Merge authoritative Runtime events with client-side optimistic messages.
 * A user message is matched once by text and a nearby timestamp so repeated
 * identical prompts in one session are not accidentally collapsed.
 */
function mergeEventLists(authoritative: EventItem[], optimistic: EventItem[] = []) {
  const byId = new Map<string, EventItem>()
  authoritative.forEach((event) => byId.set(event.id, event))
  const matchedAuthoritative = new Set<string>()
  optimistic.forEach((event) => {
    if (byId.has(event.id)) return
    const content = normalizedUserContent(event.content)
    const match = [...byId.values()]
      .filter((candidate) => (
        (event.ack_event_id ? candidate.id === event.ack_event_id : candidate.kind === 'user')
        && !matchedAuthoritative.has(candidate.id)
        && (event.ack_event_id || (
          normalizedUserContent(candidate.content) === content
          && Math.abs(eventTime(candidate) - eventTime(event)) <= 120_000
        ))
      ))
      .sort((left, right) => Math.abs(eventTime(left) - eventTime(event)) - Math.abs(eventTime(right) - eventTime(event)))[0]
    if (match) {
      matchedAuthoritative.add(match.id)
      return
    }
    byId.set(event.id, event)
  })
  return [...byId.values()].sort((left, right) => {
    const time = eventTime(left) - eventTime(right)
    if (time !== 0) return time
    if (left.sequence !== right.sequence) return left.sequence - right.sequence
    return left.id.localeCompare(right.id)
  })
}

function removeAcknowledgedOptimistic(authoritative: EventItem[], optimistic: EventItem[]) {
  const matchedAuthoritative = new Set<string>()
  return optimistic.filter((event) => {
    const content = normalizedUserContent(event.content)
    const match = authoritative
      .filter((candidate) => (
        (event.ack_event_id ? candidate.id === event.ack_event_id : candidate.kind === 'user')
        && !matchedAuthoritative.has(candidate.id)
        && (event.ack_event_id || (
          normalizedUserContent(candidate.content) === content
          && Math.abs(eventTime(candidate) - eventTime(event)) <= 120_000
        ))
      ))
      .sort((left, right) => Math.abs(eventTime(left) - eventTime(event)) - Math.abs(eventTime(right) - eventTime(event)))[0]
    if (!match) return true
    matchedAuthoritative.add(match.id)
    return false
  })
}

function statusTone(status: string): 'running' | 'success' | 'error' | 'muted' {
  if (['queued', 'running', 'awaiting_approval', 'waiting_on_predecessor', 'cancel_requested'].includes(status)) return 'running'
  if (status === 'completed') return 'success'
  if (['failed_terminal', 'failed_recoverable', 'blocked', 'stalled'].includes(status)) return 'error'
  return 'muted'
}

function StatusDot({ status }: { status: string }) {
  return <span className={'status-dot ' + statusTone(status)} aria-label={statusLabels[status] ?? status} />
}

function renderMarkdown(source: string) {
  const html = marked.parse(source, { async: false }) as string
  return { __html: DOMPurify.sanitize(html, { USE_PROFILES: { html: true } }) }
}

function formatTokens(value: number | null | undefined) {
  return value == null ? '—' : value.toLocaleString('en-US')
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = typeof body.detail === 'object' ? body.detail?.message : body.detail
    throw new Error(detail || '请求失败 (' + response.status + ')')
  }
  return response.json() as Promise<T>
}

function ContextRing({ context, onClick, compact = false }: { context: ContextState; onClick: () => void; compact?: boolean }) {
  const ratio = context.ratio == null ? 0 : Math.min(1, Math.max(0, context.ratio))
  const overLimit = context.ratio != null && context.ratio > 1
  const label = context.ratio == null ? '—' : overLimit ? '超限' : Math.round(context.ratio * 100) + '%'
  return (
    <button className={'context-ring-button ' + (compact ? 'compact ' : '') + (overLimit ? 'over-limit' : '')} onClick={onClick} title="查看上下文用量" aria-label="查看上下文用量">
      <span className="context-ring" style={{ '--context-ratio': ratio * 360 + 'deg' } as React.CSSProperties}>
        <span>{label}</span>
      </span>
    </button>
  )
}

function ContextPopover({ context, onClose }: { context: ContextState; onClose: () => void }) {
  const used = formatTokens(context.used_tokens)
  const window = formatTokens(context.window_tokens)
  const percent = context.ratio == null ? '—' : (context.ratio * 100).toFixed(1) + '%'
  const effectiveWindow = formatTokens(context.effective_window_tokens)
  const preflightPercent = context.preflight_ratio == null ? '—' : (context.preflight_ratio * 100).toFixed(1) + '%'
  const labels: Array<[string, string, string]> = [
    ['user_input', '对话消息', 'blue'],
    ['system_prompt', '系统提示词', 'violet'],
    ['tool_calls', '工具调用', 'cyan'],
    ['tool_definitions', '工具定义', 'cyan'],
    ['llm_output', '模型输出', 'green'],
    ['summary', '压缩摘要', 'gray'],
  ]
  const categoryTotal = Object.values(context.categories ?? {}).reduce((sum, value) => sum + Math.max(0, value), 0)
  const compaction = context.compaction
  const memory = context.capabilities?.memory
  const safety = context.capabilities?.safety
  const contextStatus = compaction?.applied
    ? `已${compaction.tier === 'summarize' ? '总结' : compaction.tier === 'prune' ? '裁剪' : '缩减'}`
    : '未触发'
  const capabilityStatus = (value?: { enabled?: boolean; initialized?: boolean; triggered?: boolean; degraded_reason?: string | null; details?: Record<string, unknown> }) => {
    if (value?.enabled === false) return '已关闭'
    // CapabilityProfile can remain enabled while a feature is explicitly
    // disabled by project configuration (for example MEMORY_ENABLED=false).
    // Prefer that durable configuration fact over the generic profile flag so
    // the WebUI does not report a disabled module as merely "待触发".
    if (value?.details && Object.prototype.hasOwnProperty.call(value.details, 'configured_enabled') && value.details.configured_enabled === false) return '已关闭'
    if (!value?.initialized) return '未初始化'
    if (value.degraded_reason) return '已降级'
    const degraded = value.details?.degraded_reasons
    if (Array.isArray(degraded) && degraded.length > 0) return '部分降级'
    return value.triggered ? '已启用' : '待触发'
  }
  return (
    <div className="context-popover" role="dialog" aria-label="上下文用量详情">
      <div className="popover-title"><span>上下文用量</span><button onClick={onClose} aria-label="关闭"><X size={16} /></button></div>
      <div className="popover-total"><strong>{used}/{window}</strong><span>{percent}</span><ChevronDown size={16} /></div>
      {context.effective_window_tokens != null && <div className="context-preflight">安全预检预算 {effectiveWindow} · {preflightPercent}</div>}
      <div className="context-bar"><span style={{ width: context.ratio == null ? '0%' : Math.min(100, context.ratio * 100) + '%' }} /></div>
      <div className="context-legend">
        <div><span className="legend-dot blue" /><span>最近一次 API 输入</span><b>{used}</b></div>
        {categoryTotal > 0
          ? labels.filter(([key]) => (context.categories?.[key] ?? 0) > 0).map(([key, label, tone]) => {
            const value = context.categories?.[key] ?? 0
            const share = ((value / categoryTotal) * 100).toFixed(1) + '%'
            return <div key={key}><span className={'legend-dot ' + tone} /><span>{label}</span><b>{formatTokens(value)} · {share}</b></div>
          })
          : <div><span className="legend-dot gray" /><span>分类明细</span><b>暂无 Runtime 数据</b></div>}
        <div><span className="legend-dot gray" /><span>窗口来源</span><b>{context.source}</b></div>
      </div>
      <div className="context-runtime-status">
        <div><span>压缩</span><b>{contextStatus}</b></div>
        <div><span>记忆</span><b>{capabilityStatus(memory)}</b></div>
        <div><span>安全</span><b>{capabilityStatus(safety)}</b></div>
      </div>
      {compaction?.error && <p className="popover-warning">压缩未完成：{compaction.error}</p>}
      <p className="popover-note">{context.note ?? '只展示 Runtime 已记录的真实数据，不推测百分比。'}</p>
    </div>
  )
}

function PermissionSelector({
  mode,
  onChange,
  disabled = false,
}: {
  mode: PermissionMode
  onChange: (mode: PermissionMode) => void
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const current = permissionModeSpecs.find((item) => item.mode === mode) ?? permissionModeSpecs[0]

  useEffect(() => {
    if (!open) return undefined
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  return (
    <div ref={rootRef} className="permission-selector">
      <button
        type="button"
        className={'permission-trigger mode-' + current.mode}
        onClick={() => setOpen((value) => !value)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        title={current.description}
      >
        <ShieldCheck size={14} />
        <span>{current.label}</span>
        <ChevronDown size={13} className={open ? 'permission-chevron open' : 'permission-chevron'} />
      </button>
      {open && <div className="permission-menu" role="menu" aria-label="权限策略">
        <div className="permission-menu-heading"><strong>权限策略</strong><span>选择后对后续运行生效</span></div>
        {permissionModeSpecs.map((item) => (
          <button
            type="button"
            key={item.mode}
            className={'permission-option mode-' + item.mode + (item.mode === current.mode ? ' selected' : '')}
            onClick={() => { setOpen(false); if (item.mode !== current.mode) onChange(item.mode) }}
            role="menuitemradio"
            aria-checked={item.mode === current.mode}
          >
            <span className="permission-option-icon"><ShieldCheck size={17} /></span>
            <span className="permission-option-copy"><strong>{item.label}</strong><small>{item.description}</small></span>
            {item.mode === current.mode && <Check size={16} className="permission-option-check" />}
          </button>
        ))}
        <p className="permission-menu-note">路径越界、敏感凭据和安全 hard-deny 规则在所有模式下继续生效。</p>
      </div>}
    </div>
  )
}

function SettingsModal({ initial, onClose, onSaved }: { initial: SettingsState; onClose: () => void; onSaved: (settings: SettingsState) => void }) {
  const [baseUrl, setBaseUrl] = useState(initial.base_url)
  const [model, setModel] = useState(initial.model)
  const [apiKey, setApiKey] = useState('')
  const [revealed, setRevealed] = useState(false)
  const [revealing, setRevealing] = useState(false)
  const [projectScoped, setProjectScoped] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)

  useEffect(() => {
    // Keep the secret out of the browser until the user explicitly asks to
    // reveal it.  The masked value is enough for the initial form state and
    // an empty API key on save means "keep the existing key" on the server.
    api<SettingsState>('/api/settings').then((value) => {
      setBaseUrl(value.base_url)
      setModel(value.model)
    }).catch(() => undefined)
  }, [])

  async function toggleReveal() {
    if (revealed) {
      setRevealed(false)
      return
    }
    if (apiKey) {
      setRevealed(true)
      return
    }
    setRevealing(true)
    try {
      const value = await api<SettingsState>('/api/settings?reveal=true')
      setApiKey(value.api_key ?? '')
      setRevealed(true)
    } catch (error) {
      setMessage({ tone: 'error', text: (error as Error).message })
    } finally {
      setRevealing(false)
    }
  }

  async function testConnection() {
    setBusy(true)
    setMessage(null)
    try {
      const result = await api<{ ok: boolean; message: string }>('/api/settings/test', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, model, api_key: apiKey || undefined }),
      })
      setMessage({ tone: result.ok ? 'ok' : 'error', text: result.message })
    } catch (error) {
      setMessage({ tone: 'error', text: (error as Error).message })
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    setBusy(true)
    setMessage(null)
    try {
      const result = await api<{ settings: SettingsState }>('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, model, api_key: apiKey || undefined, project_scoped: projectScoped }),
      })
      onSaved(result.settings)
      setMessage({ tone: 'ok', text: '设置已保存；下一次新运行会使用新配置。' })
    } catch (error) {
      setMessage({ tone: 'error', text: (error as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section className="settings-modal" role="dialog" aria-modal="true" aria-label="设置">
        <div className="modal-heading">
          <div><span className="eyebrow">WORKSPACE SETTINGS</span><h2>连接与模型</h2></div>
          <button className="icon-button" onClick={onClose} aria-label="关闭"><X size={19} /></button>
        </div>
        <p className="modal-intro">凭据仅保存在本机用户目录，不会写入 Durable Runtime 事件，也不会展示系统提示词。</p>
        <div className="settings-section-title"><span>模型连接</span><span className="settings-section-line" /></div>
        <label>Base URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" autoComplete="url" /></label>
        <label>模型名称<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="deepseek-v4-flash" /></label>
        <label>API Key
          <div className="secret-input">
            <input type={revealed ? 'text' : 'password'} value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={initial.has_api_key ? initial.api_key_masked : '输入 API key'} autoComplete="off" />
            <button type="button" onClick={() => void toggleReveal()} disabled={revealing}>{revealed ? <><EyeOff size={14} />隐藏</> : <><Eye size={14} />显示</>}</button>
          </div>
        </label>
        <label className="checkbox-row"><input type="checkbox" checked={projectScoped} onChange={(event) => setProjectScoped(event.target.checked)} /><span>仅对当前项目覆盖默认配置</span></label>
        {message && <div className={'inline-message ' + message.tone}><span>{message.tone === 'ok' ? <Check size={15} /> : <AlertCircle size={15} />}</span>{message.text}</div>}
        <div className="modal-actions"><button className="secondary-button" onClick={() => void testConnection()} disabled={busy}><RefreshCw size={15} className={busy ? 'spin' : ''} />测试连接</button><button className="primary-button" onClick={() => void save()} disabled={busy}><Check size={15} />保存设置</button></div>
      </section>
    </div>
  )
}

function CommandPalette({ items, selectedIndex, onSelect }: { items: CommandItem[]; selectedIndex: number; onSelect: (item: CommandItem) => void }) {
  if (items.length === 0) return <div className="command-palette"><div className="command-empty">没有匹配的命令</div></div>
  return (
    <div className="command-palette" role="listbox" aria-label="命令建议">
      <div className="command-palette-heading"><span>命令</span><span className="command-palette-hint">↑↓ 选择 · Enter 执行</span></div>
      {items.map((item, index) => {
        const Icon = item.icon
        return <button key={item.id} className={'command-item ' + (index === selectedIndex ? 'active' : '')} onMouseDown={(event) => { event.preventDefault(); onSelect(item) }} role="option" aria-selected={index === selectedIndex}><span className="command-icon"><Icon size={15} /></span><span className="command-copy"><strong><code>{item.command}</code> {item.label}</strong><small>{item.description}</small></span><ChevronRight size={14} className="command-chevron" /></button>
      })}
    </div>
  )
}

function friendlyError(event: EventItem) {
  const raw = String(event.payload?.error_kind ?? event.payload?.reason ?? '').trim()
  const detail = raw && raw !== event.event_type ? ': ' + raw : ''
  switch (event.event_type) {
    case 'ActionFailed': return '工具执行失败' + detail
    case 'ActionOutcomeUnknown': return '工具结果待确认' + detail
    case 'RunStalled': return '任务暂时暂停，可继续运行' + detail
    case 'StallDiagnosisRecorded': return '任务暂停原因已记录' + detail
    case 'ModelInvocationFailed': return '模型请求失败' + detail
    case 'RunCancelled': return '任务已停止' + detail
    default: return (runtimeEventLabel(event) || '运行时错误') + detail
  }
}

function toolStatusLabel(value: string | undefined) {
  if (!value) return '处理中'
  return runtimeStatusLabels[value] ?? statusLabels[value] ?? value
}

function TurnProcess({ events, live, onCopy }: { events: EventItem[]; live: boolean; onCopy: (text: string) => void }) {
  if (events.length === 0) return null
  const toolCount = events.filter((event) => event.kind === 'tool').length
  const messageCount = events.filter((event) => event.kind === 'assistant').length
  const summary = [
    '已思考',
    toolCount > 0 ? `${toolCount} 次工具调用` : '',
    messageCount > 0 ? `${messageCount} 条过程消息` : '',
  ].filter(Boolean).join(' · ')
  return (
    <details className="turn-process" open={live}>
      <summary className="turn-process-summary">
        <span className="turn-process-chevron"><ChevronRight size={14} /></span>
        <span>{summary}</span>
        {live && <span className="turn-process-live">进行中</span>}
      </summary>
      <div className="turn-process-body">
        {events.map((event) => <MessageCard event={event} key={event.id} onCopy={onCopy} />)}
      </div>
    </details>
  )
}

function MessageCard({ event, onCopy }: { event: EventItem; onCopy: (text: string) => void }) {
  if (event.kind === 'user') {
    return <article className="message user-message"><div className="message-avatar user-avatar">你</div><div className="message-body"><div className="message-label">你</div><div className="message-content">{event.content}</div></div></article>
  }
  if (event.kind === 'assistant') {
    return <article className="message assistant-message"><div className="message-avatar assistant-avatar"><Sparkles size={15} /></div><div className="message-body"><div className="message-header"><div className="message-label">cc-harness</div>{event.content && <button className="message-action" onClick={() => onCopy(event.content ?? '')} title="复制回复" aria-label="复制回复"><Copy size={14} /></button>}</div><div className="markdown message-content" dangerouslySetInnerHTML={renderMarkdown(event.content ?? '')} /></div></article>
  }
  if (event.kind === 'tool') {
    return <details className="tool-card"><summary><span className="tool-summary"><span className="tool-icon"><Play size={13} /></span><span className="tool-name">{event.tool_name || '工具调用'}</span><span className={'tool-status ' + (event.status === 'succeeded' ? 'ok' : event.status === 'failed' ? 'failed' : '')}>{toolStatusLabel(event.status)}</span></span><ChevronDown size={15} /></summary><pre>{event.content || '（没有可展示的输出）'}</pre></details>
  }
  if (event.kind === 'error') {
    return <div className="event-notice error"><CircleAlert size={16} /><span>{friendlyError(event)}</span></div>
  }
  if (event.kind === 'outcome') {
    return <div className="event-notice outcome"><CircleCheck size={16} /><span>Runtime 已记录运行结果</span></div>
  }
  return <div className="event-notice"><span className="event-line" /><span>{runtimeEventLabel(event)}</span></div>
}

type PendingApproval = {
  approvalId: string
  digest: string
  actionId?: string
  scope: string[]
}

function ApprovalCard({ approval, onApprove, onReject }: { approval: PendingApproval; onApprove: () => void; onReject: () => void }) {
  const action = approval.actionId || '需要授权的动作'
  return (
    <div className="approval-card" role="region" aria-label="待处理审批">
      <div className="approval-heading"><ShieldCheck size={17} /><span><strong>需要你的批准</strong><small>Runtime 正在等待这项本机操作</small></span></div>
      <div className="approval-detail"><span>动作</span><b>{action}</b>{approval.scope.length > 0 && <><span>范围</span><b className="approval-scope" title={approval.scope.join('\n')}>{approval.scope.join('、')}</b></>}</div>
      <div className="approval-actions"><button className="secondary-button" onClick={onReject}>拒绝</button><button className="primary-button" onClick={onApprove}>允许一次</button></div>
    </div>
  )
}

function App() {
  const [project, setProject] = useState<{ root: string } | null>(null)
  const [settings, setSettings] = useState<SettingsState>({ base_url: '', model: '', permission_mode: 'default', has_api_key: false, api_key_masked: '' })
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [optimisticEvents, setOptimisticEvents] = useState<Record<string, EventItem[]>>({})
  const [context, setContext] = useState<ContextState>(emptyContext)
  const [executor, setExecutor] = useState<ExecutorState>(emptyExecutor)
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([])
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [contextOpen, setContextOpen] = useState(false)
  const [contextAnchor, setContextAnchor] = useState<'bottom' | null>(null)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(false)
  const [sessionQuery, setSessionQuery] = useState('')
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({})
  const [commandIndex, setCommandIndex] = useState(0)
  const [cursorPosition, setCursorPosition] = useState(0)
  const [showNewMessages, setShowNewMessages] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return window.localStorage.getItem('cc-harness-theme') === 'light' ? 'light' : 'dark'
    } catch {
      return 'dark'
    }
  })
  const eventSource = useRef<EventSource | null>(null)
  const activeSessionRef = useRef<string | null>(null)
  const loadGeneration = useRef(0)
  const refreshTimer = useRef<number | null>(null)
  const conversationRef = useRef<HTMLElement | null>(null)
  const stickToBottom = useRef(true)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const searchRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { window.localStorage.setItem('cc-harness-theme', theme) } catch { /* localStorage may be unavailable */ }
  }, [theme])

  useEffect(() => {
    if (!toast) return undefined
    const timer = window.setTimeout(() => setToast(null), 3600)
    return () => window.clearTimeout(timer)
  }, [toast])

  const refresh = useCallback(async () => {
    try {
      const boot = await api<Bootstrap>('/api/bootstrap')
      setProject(boot.project)
      setSettings({ ...boot.settings, permission_mode: normalizePermissionMode(boot.settings.permission_mode) })
      setSessions(boot.sessions)
      if (boot.initial_prompt) setDraft((current) => current || boot.initial_prompt || '')
      setActiveSession((current) => current ?? boot.sessions[0]?.run_id ?? null)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const loadSession = useCallback(async (runId: string) => {
    const requestGeneration = ++loadGeneration.current
    try {
      const [timeline, state, list] = await Promise.all([
        api<{ events: EventItem[] }>('/api/sessions/' + encodeURIComponent(runId) + '/timeline'),
        api<{ project_root?: string; context: ContextState; status: string; executor?: ExecutorState; projection?: { approvals?: Array<{ approval_id: string; action_args_digest: string; action_id?: string; scope?: string[]; status: string }> } }>('/api/sessions/' + encodeURIComponent(runId)),
        api<{ sessions: Session[] }>('/api/sessions'),
      ])
      // A session can be switched while these three requests are in flight.
      // Never let a stale response replace the newly selected conversation.
      if (requestGeneration !== loadGeneration.current || activeSessionRef.current !== runId) return
      // Union the snapshot with events that arrived through SSE while the
      // request was in flight; a slow timeline response must not erase a live
      // assistant/tool event.
      setEvents((current) => mergeEventLists(current, timeline.events))
      setOptimisticEvents((current) => {
        const pending = removeAcknowledgedOptimistic(timeline.events, current[runId] ?? [])
        if (pending.length === 0) {
          const next = { ...current }
          delete next[runId]
          return next
        }
        return { ...current, [runId]: pending }
      })
      if (state.project_root) {
        setProject((current) => current?.root === state.project_root ? current : { root: state.project_root! })
      }
      setContext(state.context)
      setExecutor(state.executor ?? emptyExecutor)
      setSessions(list.sessions)
      setPendingApprovals((state.projection?.approvals ?? []).filter((item) => item.status === 'requested').map((item) => ({ approvalId: item.approval_id, digest: item.action_args_digest, actionId: item.action_id, scope: item.scope ?? [] })))
      setError(null)
    } catch (reason) {
      // A stale request may fail after the user has already switched sessions.
      // Keep that failure from surfacing over the currently selected session.
      if (requestGeneration !== loadGeneration.current || activeSessionRef.current !== runId) return
      setError((reason as Error).message)
    }
  }, [])

  const scheduleSessionRefresh = useCallback((runId: string) => {
    if (activeSessionRef.current !== runId) return
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null
      if (activeSessionRef.current === runId) void loadSession(runId)
    }, 120)
  }, [loadSession])

  useEffect(() => {
    activeSessionRef.current = activeSession
    loadGeneration.current += 1
    setEvents([])
    setPendingApprovals([])
    setContext(emptyContext)
    setShowNewMessages(false)
    stickToBottom.current = true
    if (!activeSession) {
      eventSource.current?.close()
      return undefined
    }
    const streamSession = activeSession
    void loadSession(streamSession)
    eventSource.current?.close()
    const source = new EventSource('/api/sessions/' + encodeURIComponent(streamSession) + '/events')
    eventSource.current = source
    source.addEventListener('runtime', (message) => {
      try {
        const next = JSON.parse((message as MessageEvent).data) as EventItem
        setEvents((current) => mergeEventLists(current, [next]))
        setOptimisticEvents((current) => {
          const pending = removeAcknowledgedOptimistic([next], current[streamSession] ?? [])
          if (pending.length === 0) {
            const nextState = { ...current }
            delete nextState[streamSession]
            return nextState
          }
          return { ...current, [streamSession]: pending }
        })
        scheduleSessionRefresh(streamSession)
      } catch {
        setError('实时事件格式不可用，已等待下一条事件。')
      }
    })
    source.onerror = () => { /* EventSource retries with Last-Event-ID automatically. */ }
    return () => {
      source.close()
      if (refreshTimer.current !== null) {
        window.clearTimeout(refreshTimer.current)
        refreshTimer.current = null
      }
    }
  }, [activeSession, loadSession, scheduleSessionRefresh])

  const displayEvents = useMemo(
    () => mergeEventLists(events, activeSession ? (optimisticEvents[activeSession] ?? []) : []),
    [activeSession, events, optimisticEvents],
  )

  useEffect(() => {
    if (displayEvents.length === 0) return
    if (!stickToBottom.current) {
      setShowNewMessages(true)
      return
    }
    window.requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }))
  }, [displayEvents.length])

  function handleConversationScroll() {
    const element = conversationRef.current
    if (!element) return
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    const atBottom = distanceFromBottom <= 80
    stickToBottom.current = atBottom
    if (atBottom) setShowNewMessages(false)
  }

  function jumpToLatest() {
    stickToBottom.current = true
    setShowNewMessages(false)
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }

  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey
      if (modifier && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        searchRef.current?.focus()
      }
      if (modifier && event.key === ',') {
        event.preventDefault()
        setSettingsOpen(true)
      }
      if (event.key === 'Escape' && contextOpen) {
        setContextOpen(false)
        setContextAnchor(null)
      }
    }
    window.addEventListener('keydown', onShortcut)
    return () => window.removeEventListener('keydown', onShortcut)
  }, [contextOpen])

  const active = sessions.find((session) => session.run_id === activeSession) ?? null
  const filteredSessions = useMemo(() => {
    const query = sessionQuery.trim().toLowerCase()
    if (!query) return sessions
    return sessions.filter((session) => session.title.toLowerCase().includes(query) || session.status.toLowerCase().includes(query) || (session.project_root ?? '').toLowerCase().includes(query))
  }, [sessionQuery, sessions])
  const projectGroups = useMemo(() => {
    const groups = new Map<string, Session[]>()
    filteredSessions.forEach((session) => {
      const root = session.project_root ?? project?.root ?? '未选择项目'
      groups.set(root, [...(groups.get(root) ?? []), session])
    })
    if (project && !sessionQuery.trim() && !groups.has(project.root)) groups.set(project.root, [])
    return [...groups.entries()].map(([root, groupSessions]) => ({ root, sessions: groupSessions }))
  }, [filteredSessions, project, sessionQuery])
  const conversationEvents = useMemo(
    () => displayEvents.filter((event) => event.kind === 'user' || event.kind === 'assistant' || event.kind === 'tool'),
    [displayEvents],
  )
  const conversationTurns = useMemo(() => groupConversationTurns(conversationEvents), [conversationEvents])
  const liveStatusEvents = useMemo(
    () => displayEvents.filter((event) => event.kind === 'status' || event.kind === 'outcome' || event.kind === 'error').slice(-8).reverse(),
    [displayEvents],
  )
  const commandQueryMatch = draft.slice(0, Math.max(0, Math.min(cursorPosition, draft.length))).match(/(?:^|\s)\/([^\s]*)$/)
  const commandQuery = commandQueryMatch?.[1].toLowerCase() ?? ''
  const visibleCommands = useMemo(() => commandItems.filter((item) => !commandQuery || item.command.slice(1).startsWith(commandQuery) || item.label.includes(commandQuery)), [commandQuery])
  const commandPaletteOpen = commandQueryMatch !== null
  const status = active?.status ?? 'idle'
  const connectionLabel = executor.degraded
    ? '沙箱不可用 · 已降级本机'
    : executor.backend === 'sandbox'
      ? 'OpenSandbox 正常'
      : executor.backend === 'native'
        ? '本机执行'
        : '等待 Runtime'
  const projectName = project?.root.split(/[\\/]/).filter(Boolean).pop() ?? '未选择项目'

  function notify(message: string) {
    setToast(message)
  }

  function toggleContext(anchor: 'bottom') {
    if (contextOpen && contextAnchor === anchor) {
      setContextOpen(false)
      setContextAnchor(null)
      return
    }
    setContextAnchor(anchor)
    setContextOpen(true)
  }

  function toggleProjectGroup(root: string) {
    setCollapsedProjects((current) => ({ ...current, [root]: !(current[root] ?? false) }))
  }

  async function activateSession(session: Session) {
    if (!session.project_root || project?.root === session.project_root) {
      setActiveSession(session.run_id)
      return
    }
    try {
      const result = await api<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ path: session.project_root }) })
      if (!result.selected) return
      setProject(result.project)
      if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
      setSessions(result.sessions)
      setEvents([])
      setOptimisticEvents({})
      setActiveSession(session.run_id)
      setError(null)
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function chooseProject() {
    try {
      const result = await api<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ picker: true }) })
      if (result.selected) {
        setProject(result.project)
        if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
        setSessions(result.sessions)
        setActiveSession(null)
        setEvents([])
        setOptimisticEvents({})
        setError(null)
        notify('已切换到 ' + (result.project?.root ?? '本地项目'))
      }
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function chooseProjectManually() {
    const path = window.prompt('输入本机项目文件夹的完整路径')
    if (!path) return
    try {
      const result = await api<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ path }) })
      if (result.selected) {
        setProject(result.project)
        if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
        setSessions(result.sessions)
        setActiveSession(null)
        setEvents([])
        setOptimisticEvents({})
        setError(null)
        notify('已选择项目')
      }
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function changePermissionMode(mode: PermissionMode) {
    const previous = settings.permission_mode
    setSettings((current) => ({ ...current, permission_mode: mode }))
    try {
      const result = await api<{ settings: SettingsState }>('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ permission_mode: mode, project_scoped: Boolean(project) }),
      })
      setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
      const isActive = Boolean(active && ['queued', 'running', 'awaiting_approval', 'waiting_on_predecessor', 'cancel_requested'].includes(active.status))
      notify(isActive
        ? permissionModeSpecs.find((item) => item.mode === mode)?.label + '已保存；当前运行结束后生效'
        : '已切换为 ' + (permissionModeSpecs.find((item) => item.mode === mode)?.label ?? mode))
    } catch (reason) {
      setSettings((current) => ({ ...current, permission_mode: previous }))
      setError((reason as Error).message)
    }
  }

  async function sendMessage() {
    if (!project) {
      setError('请先选择本地项目文件夹')
      notify('选择项目后才能开始工作')
      return
    }
    const text = draft.trim()
    if (!text || sending) return
    const requestedSession = activeSession
    setSending(true)
    setError(null)
    try {
      const result = await api<{ session_id: string; sequence?: number; message_event_id?: string; continuation?: 'same_run' | 'child_run' }>('/api/messages', { method: 'POST', body: JSON.stringify({ text, session_id: requestedSession, project_root: project?.root ?? null }) })
      const sessionId = result.session_id
      // The Runtime is authoritative, but its event projection can arrive a
      // little after the command receipt.  Render the user's message now and
      // reconcile it with the durable event when SSE/timeline catches up.
      const optimisticEvent: EventItem = {
        id: 'optimistic-' + (globalThis.crypto?.randomUUID?.() ?? String(Date.now()) + '-' + Math.random().toString(16).slice(2)),
        event_id: 'optimistic',
        run_id: sessionId,
        sequence: result.sequence ?? 0,
        event_type: requestedSession
          ? (result.continuation === 'same_run' ? 'RunResumed' : 'FollowUpQueued')
          : 'RunCreated',
        occurred_at: new Date().toISOString(),
        kind: 'user',
        content: text,
        payload: {},
        optimistic: true,
        ack_event_id: result.message_event_id,
      }
      setOptimisticEvents((current) => ({
        ...current,
        [sessionId]: mergeEventLists(current[sessionId] ?? [], [optimisticEvent]),
      }))
      setDraft('')
      setCursorPosition(0)
      setActiveSession(sessionId)
      // A successful receipt must not be reported as a failed send merely
      // because the sidebar refresh is temporarily unavailable.
      try {
        const list = await api<{ sessions: Session[] }>('/api/sessions')
        setSessions(list.sessions)
      } catch {
        scheduleSessionRefresh(sessionId)
      }
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSending(false)
    }
  }

  async function stopSession() {
    if (!activeSession) return
    try {
      await api('/api/sessions/' + encodeURIComponent(activeSession) + '/stop', { method: 'POST' })
      await loadSession(activeSession)
      const list = await api<{ sessions: Session[] }>('/api/sessions')
      setSessions(list.sessions)
      notify('已请求停止当前运行')
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function resumeSession() {
    if (!activeSession) return
    try {
      await api('/api/sessions/' + encodeURIComponent(activeSession) + '/resume', { method: 'POST' })
      await loadSession(activeSession)
      notify('已从最近检查点继续')
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function approve(approval: { approvalId: string; digest: string }) {
    if (!activeSession) return
    try {
      await api('/api/sessions/' + encodeURIComponent(activeSession) + '/approvals/' + encodeURIComponent(approval.approvalId) + '/approve', { method: 'POST', body: JSON.stringify({ action_args_digest: approval.digest }) })
      await loadSession(activeSession)
      notify('动作已批准')
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function reject(approval: PendingApproval) {
    if (!activeSession) return
    try {
      await api('/api/sessions/' + encodeURIComponent(activeSession) + '/approvals/' + encodeURIComponent(approval.approvalId) + '/reject', { method: 'POST', body: JSON.stringify({ reason: 'WebUI 用户拒绝' }) })
      await loadSession(activeSession)
      notify('已拒绝该动作')
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function copyText(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      notify('已复制到剪贴板')
    } catch {
      notify('当前环境不允许访问剪贴板')
    }
  }

  function selectCommand(item: CommandItem) {
    setDraft('')
    setCursorPosition(0)
    setCommandIndex(0)
    if (item.id === 'new') {
      setActiveSession(null)
      setEvents([])
      setOptimisticEvents({})
      setPendingApprovals([])
      setContext(emptyContext)
      notify('已准备新会话')
    } else if (item.id === 'project') {
      void chooseProject()
    } else if (item.id === 'settings') {
      setSettingsOpen(true)
    } else if (item.id === 'status') {
      if (activeSession) void loadSession(activeSession)
      notify(active ? '状态：' + (statusLabels[active.status] ?? active.status) : '当前没有活动会话')
    } else if (item.id === 'resume') {
      if (activeSession) void resumeSession()
      else notify('当前没有可恢复的会话')
    } else if (item.id === 'clear') {
      setEvents([])
      setContext(emptyContext)
      notify('已清空当前视图；Durable Runtime 事件仍可审计')
    } else if (item.id === 'help') {
      notify('可用命令：/new、/project、/status、/resume、/settings、/clear')
    }
  }

  function usePrompt(prompt: string) {
    setDraft(prompt)
    setCursorPosition(prompt.length)
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus()
      textareaRef.current?.setSelectionRange(prompt.length, prompt.length)
    })
  }

  function syncCursorPosition(event: React.SyntheticEvent<HTMLTextAreaElement>) {
    setCursorPosition(event.currentTarget.selectionStart ?? event.currentTarget.value.length)
  }

  function handleComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (commandPaletteOpen) {
      if (event.key === 'Escape') {
        event.preventDefault()
        setDraft('')
        setCursorPosition(0)
        return
      }
      if (visibleCommands.length === 0) return
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setCommandIndex((value) => (value + 1) % visibleCommands.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setCommandIndex((value) => (value - 1 + visibleCommands.length) % visibleCommands.length)
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        selectCommand(visibleCommands[commandIndex] ?? visibleCommands[0])
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void sendMessage()
    }
  }

  if (loading) return <div className="loading-screen"><LoaderCircle className="spin" size={28} /><span>正在连接本地 Runtime…</span></div>

  const shellClass = ['app-shell', leftCollapsed ? 'left-collapsed' : '', rightCollapsed ? 'right-collapsed' : ''].filter(Boolean).join(' ')
  return (
    <div className={shellClass}>
      <aside className="left-sidebar">
        <div className="brand-row">
          <div className="brand-mark"><img src="/mascot.png" alt="" /></div>
          <span className="brand-name">cc-harness</span>
          <span className="brand-live" title="本地 WebUI 已连接" />
          <button className="icon-button sidebar-menu" onClick={() => setLeftCollapsed(true)} title="收起侧栏"><PanelLeftClose size={16} /></button>
        </div>
        <button className="workspace-switcher" onClick={chooseProject}>
          <span className="workspace-icon"><Folder size={15} /></span>
          <span className="workspace-copy"><small>当前项目</small><strong>{projectName}</strong></span>
          <ChevronDown size={15} />
        </button>
        <div className="session-heading"><div><span>会话</span><small>{sessions.length ? sessions.length + ' 个运行' : '开始你的第一个任务'}</small></div><button className="icon-button" onClick={() => { setActiveSession(null); setEvents([]); setOptimisticEvents({}); setContext(emptyContext); setPendingApprovals([]) }} title="新会话"><MessageSquarePlus size={17} /></button></div>
        <div className="session-search"><Search size={14} /><input ref={searchRef} value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} placeholder="搜索会话" aria-label="搜索会话" /><kbd>Ctrl K</kbd></div>
        <div className="session-list">
          {projectGroups.length === 0 && <div className="empty-sessions"><History size={18} /><p>{sessionQuery ? '没有匹配的会话' : '还没有会话'}</p><span>{sessionQuery ? '换个关键词试试' : '发送第一条任务开始工作'}</span></div>}
          {projectGroups.map(({ root, sessions: groupSessions }) => {
            const collapsed = collapsedProjects[root] ?? false
            return <section className="project-group" key={root}>
              <button className="project-group-header" onClick={() => toggleProjectGroup(root)} title={root} aria-expanded={!collapsed}>
                <span className="project-group-icon"><Folder size={14} /></span>
                <span className="project-group-copy"><strong>{projectDisplayName(root)}</strong><small>{root}</small></span>
                <span className="project-group-count">{groupSessions.length}</span>
                <ChevronDown size={14} className={'project-group-chevron ' + (collapsed ? 'collapsed' : '')} />
              </button>
              {!collapsed && <div className="project-group-sessions">
                {groupSessions.length === 0
                  ? <div className="project-group-empty">暂无会话</div>
                  : groupSessions.map((session) => <button key={session.run_id} className={'session-item ' + (activeSession === session.run_id ? 'selected' : '')} onClick={() => void activateSession(session)}><StatusDot status={session.status} /><span className="session-title">{session.title}</span><span className="session-status">{statusLabels[session.status] ?? session.status}<span className="session-sequence"> · {session.sequence} 事件</span></span><MoreHorizontal size={15} className="session-more" /></button>)}
              </div>}
            </section>
          })}
        </div>
        <div className="sidebar-bottom">
          <button className="project-chip" onClick={chooseProject}><FolderOpen size={15} /><span>{project ? project.root : '选择项目文件夹'}</span><ChevronRight size={14} /></button>
          <div className="sidebar-actions"><button className="settings-button" onClick={() => setSettingsOpen(true)}><Settings size={17} /><span>设置</span></button><button className="collapse-button" onClick={() => setLeftCollapsed(true)} title="收起侧栏"><PanelLeftClose size={16} /></button></div>
        </div>
      </aside>
      {leftCollapsed && <button className="expand-sidebar" onClick={() => setLeftCollapsed(false)} title="展开侧栏"><PanelLeftOpen size={18} /></button>}

      <main className="main-panel">
        <header className="topbar">
          <div className="breadcrumb"><span className="topbar-project">{projectName}</span><ChevronRight size={14} /><span className="topbar-title">{active ? active.title : '新会话'}</span></div>
          <div className="topbar-actions"><span className="topbar-live"><span className="pulse-dot" />本地 Runtime</span><button className="topbar-icon" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')} title={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}>{theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}</button><button className="topbar-icon" onClick={() => setRightCollapsed((value) => !value)} title={rightCollapsed ? '打开运行面板' : '收起运行面板'}>{rightCollapsed ? <PanelRightOpen size={17} /> : <PanelRightClose size={17} />}</button><button className="topbar-icon" onClick={() => setSettingsOpen(true)} title="设置"><Settings size={16} /></button></div>
        </header>
              <section ref={conversationRef} className="conversation" aria-live="polite" onScroll={handleConversationScroll}>
            <div className="conversation-inner">
              {!project && <div className="welcome-state no-project"><div className="welcome-orbit"><Sparkles size={25} /></div><span className="welcome-kicker">LOCAL AGENT WORKSPACE</span><h1>把你的项目交给 cc-harness</h1><p>先选择一个本机项目文件夹，主 Agent 才能在安全边界内读取和修改代码。<br />浏览器关闭不会停止已提交的 Durable Run。</p><div className="welcome-actions"><button className="primary-button" onClick={chooseProject}><FolderOpen size={16} />选择项目文件夹</button><button className="text-button" onClick={chooseProjectManually}>手动输入路径</button></div><div className="welcome-note"><ShieldCheck size={14} />本地处理 · 可恢复检查点 · 可审计事件</div></div>}
              {project && conversationTurns.length === 0 && <div className="welcome-state project-ready"><div className="welcome-orbit small"><Bot size={23} /></div><span className="welcome-kicker">项目已就绪 · {projectName}</span><h1>今天要完成什么？</h1><p>描述目标，cc-harness 会先读取项目状态，再由 Durable Runtime 编排执行。</p><div className="suggestion-grid"><button onClick={() => usePrompt('先检查这个项目的结构、依赖和测试入口，然后给出改进建议。')}><FileCode2 size={16} /><span>了解项目结构</span><ChevronRight size={14} /></button><button onClick={() => usePrompt('运行现有测试，定位失败原因并修复最小范围的问题。')}><CircleCheck size={16} /><span>运行测试并修复</span><ChevronRight size={14} /></button><button onClick={() => usePrompt('审查当前改动，重点检查安全性、错误处理和可维护性。')}><ShieldCheck size={16} /><span>审查当前改动</span><ChevronRight size={14} /></button></div></div>}
              <div className="message-stack">{conversationTurns.map((turn, index) => <section className="conversation-turn" key={turn.id}>{turn.user && <MessageCard event={turn.user} onCopy={(text) => void copyText(text)} />}<TurnProcess events={turn.process} live={active != null && ['running', 'awaiting_approval'].includes(active.status) && index === conversationTurns.length - 1} onCopy={(text) => void copyText(text)} />{turn.assistant && <MessageCard event={turn.assistant} onCopy={(text) => void copyText(text)} />}</section>)}{active && ['running', 'queued'].includes(active.status) && <div className="typing-indicator"><span /><span /><span /><em>Runtime 正在工作</em></div>}{pendingApprovals.length > 0 && <ApprovalCard approval={pendingApprovals[0]} onApprove={() => void approve(pendingApprovals[0])} onReject={() => void reject(pendingApprovals[0])} />}<div ref={bottomRef} /></div>
            </div>
            {showNewMessages && <button className="jump-to-latest" onClick={jumpToLatest}><ChevronDown size={14} />跳到最新消息</button>}
          </section>
        {error && <div className="error-banner"><AlertCircle size={16} /><span>{error}</span><button onClick={() => setError(null)} aria-label="关闭错误"><X size={14} /></button></div>}

        <footer className="composer-wrap">
          <div className="composer-label-row"><span>{active ? '继续与 cc-harness 协作' : '新的任务'}</span><span className="composer-shortcut"><Keyboard size={13} /> Enter 发送 · Shift+Enter 换行</span></div>
          <div className="composer">
            {commandPaletteOpen && <CommandPalette items={visibleCommands} selectedIndex={commandIndex} onSelect={selectCommand} />}
            <div className="composer-toolbar"><button className={'composer-project ' + (!project ? 'needs-project' : '')} onClick={chooseProject}><FolderOpen size={16} /><span>{project ? projectName : '选择项目文件夹'}</span><ChevronDown size={14} /></button><div className="composer-toolbar-right"><span className="composer-mode"><ShieldCheck size={13} />本地 Runtime</span></div></div>
             <textarea ref={textareaRef} value={draft} onChange={(event) => { setDraft(event.target.value); setCursorPosition(event.currentTarget.selectionStart ?? event.currentTarget.value.length); setCommandIndex(0) }} onKeyDown={handleComposerKeyDown} onClick={(event) => { syncCursorPosition(event); if (!project) notify('请先选择本地项目文件夹') }} onKeyUp={syncCursorPosition} onSelect={syncCursorPosition} disabled={!project || sending} placeholder={project ? '描述要完成的任务，或输入 / 查看命令…' : '请先选择本地项目文件夹'} rows={3} aria-label="任务输入框" />
          <div className="composer-footer"><div className="composer-footer-left"><PermissionSelector mode={settings.permission_mode} onChange={(mode) => void changePermissionMode(mode)} /><span className="privacy-note"><ShieldCheck size={13} />内容只在本机 Runtime 处理</span></div><div className="composer-actions">{active && ['running', 'queued', 'awaiting_approval'].includes(active.status) && <button className="stop-button" onClick={() => void stopSession()}><CircleStop size={15} />停止</button>}{active && ['cancelled', 'stalled', 'failed_recoverable', 'blocked'].includes(active.status) && <button className="secondary-button compact" onClick={() => void resumeSession()}><RotateCcw size={15} />{active.status === 'blocked' ? '确认继续' : '继续'}</button>}<button className="send-button" onClick={() => void sendMessage()} disabled={!project || !draft.trim() || sending} aria-label="发送">{sending ? <LoaderCircle size={16} className="spin" /> : <Send size={16} />}<span>{sending ? '提交中' : '发送'}</span></button></div></div>
          </div>
        </footer>
        <div className="bottom-statusbar">
          <div className="status-cluster"><span className="status-item"><StatusDot status={status} /><span>运行</span><strong>{status === 'idle' ? '待命' : statusLabels[status] ?? status}</strong></span><span className="status-divider" /><span className="status-item"><span className="status-check"><CircleCheck size={13} /></span><span>连接</span><strong className={executor.degraded ? 'status-warning' : 'status-ok'} title={executor.fallback_reason ?? undefined}>{connectionLabel}</strong></span><span className="status-divider" /><span className="status-item"><ShieldCheck size={13} /><span>权限</span><strong title={permissionModeSpecs.find((item) => item.mode === settings.permission_mode)?.description}>{permissionModeSpecs.find((item) => item.mode === settings.permission_mode)?.label ?? '请求批准'}</strong></span></div>
           <div className="bottom-status-actions"><div className="bottom-context-anchor"><ContextRing context={context} onClick={() => toggleContext('bottom')} compact />{contextOpen && contextAnchor === 'bottom' && <ContextPopover context={context} onClose={() => { setContextOpen(false); setContextAnchor(null) }} />}</div><button className="model-status" onClick={() => setSettingsOpen(true)}><span>模型</span><strong>{settings.model || '未配置'}</strong><ChevronDown size={14} /></button><a className="created-by" href="https://deerflow.tech" target="_blank" rel="noreferrer">Created By Deerflow</a></div>
        </div>
      </main>

      <aside className="right-sidebar">
        <div className="inspector-header"><div><span>运行面板</span><small>可观测状态</small></div><button className="icon-button" onClick={() => setRightCollapsed(true)} title="收起运行面板"><PanelRightClose size={16} /></button></div>
        <div className="inspector-scroll">
          <section className="inspector-card runtime-overview"><div className="card-eyebrow"><span className="pulse-dot" /> DURABLE RUNTIME</div><div className="runtime-state"><StatusDot status={status} /><div><strong>{status === 'idle' ? '等待输入' : statusLabels[status] ?? status}</strong><span>{active ? '事件序号 ' + active.sequence : '选择项目后开始'}</span></div></div><div className="inspector-row"><span>项目</span><strong>{project ? '已选择' : '未选择'}</strong></div><div className="inspector-row"><span>活动会话</span><strong>{active ? '已连接' : '—'}</strong></div><div className="inspector-row"><span>执行后端</span><strong className={executor.degraded ? 'warn-text' : ''}>{connectionLabel}</strong></div><div className="inspector-row"><span>审批</span><strong className={pendingApprovals.length > 0 ? 'warn-text' : ''}>{pendingApprovals.length > 0 ? pendingApprovals.length + ' 项待处理' : '无待处理'}</strong></div></section>
          <section className="inspector-card live-status-card"><div className="card-heading"><div><span className="card-eyebrow">实时状态</span><small>{active ? active.title : '选择会话后显示'}</small></div><StatusDot status={status} /></div><div className="live-status-summary"><StatusDot status={status} /><div><strong>{status === 'idle' ? '等待输入' : statusLabels[status] ?? status}</strong><span>{active ? '事件序号 ' + active.sequence : '当前没有活动会话'}</span></div></div>{liveStatusEvents.length === 0 ? <div className="activity-empty"><Info size={15} /><span>任务运行后，这里会显示实时状态。</span></div> : <div className="runtime-timeline">{liveStatusEvents.map((event) => <div className={'runtime-timeline-row ' + event.kind} key={event.id}><span className="runtime-timeline-mark" /><div><strong>{runtimeEventLabel(event)}</strong><small>事件 #{event.sequence}</small></div></div>)}</div>}</section>
        </div>
        <div className="inspector-model"><div className="model-chip"><span className="model-chip-dot" /><div><small>当前模型</small><strong>{settings.model || '未配置'}</strong></div></div><button className="icon-button" onClick={() => setSettingsOpen(true)} title="设置"><Settings size={16} /></button></div>
      </aside>
      {settingsOpen && <SettingsModal initial={settings} onClose={() => setSettingsOpen(false)} onSaved={(value) => setSettings(value)} />}
      {toast && <div className="toast" role="status"><Check size={15} />{toast}</div>}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
