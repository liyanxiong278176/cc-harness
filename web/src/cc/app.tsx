import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import type { LucideIcon } from 'lucide-react'
import {
  AlertCircle, Bot, Check, ChevronDown, ChevronRight,
  CircleAlert, CircleCheck, CircleStop, Command, Copy, Eye, EyeOff,
  Folder, FolderOpen, History, Info, Keyboard, LoaderCircle,
  MessageSquarePlus, Moon, PanelLeftClose, PanelLeftOpen,
  PanelRightClose, PanelRightOpen, Play, RefreshCw, RotateCcw, Search,
  Send, Settings, ShieldCheck, Sparkles, Sun, Trash2, X,
} from 'lucide-react'
import { webApi, webEventsUrl } from './api'
// The application is kept under `cc/` so the Web entry point remains a small
// adapter.  Visual tokens and layout styles live at the Vite root and are
// shared by the migrated DeepSeek-style shell.

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
  scheduler?: SchedulerState
}
type SchedulerState = {
  mode: 'local' | 'external' | 'idle' | string
  running: boolean
  label: string
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
type StreamItem = {
  type: 'stream_delta' | 'stream_gap'
  run_id: string
  root_run_id?: string
  segment?: number
  chunk?: number
  kind?: 'content' | 'tool_call_delta' | 'done'
  text?: string
  tool_name?: string | null
  finish_reason?: string | null
  tool_call_count?: number
  usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number } | null
  live_id?: number
  reason?: string
  ts?: number
}
type StreamingState = {
  run_id: string
  root_run_id: string
  segment: number
  chunk: number
  text: string
  phase: 'content' | 'tool' | 'done' | 'gap' | 'stopped' | 'failed'
  tool_name?: string | null
  finish_reason?: string | null
  updated_at: number
  frozen?: boolean
}

function reduceStreamItem(
  current: Record<string, StreamingState>,
  next: StreamItem,
  streamSession: string,
  now: number,
) {
  if (!next.run_id) return current
  const previous = current[next.run_id]
  const segment = Number(next.segment ?? previous?.segment ?? 0)
  const chunk = Number(next.chunk ?? previous?.chunk ?? 0)
  if (previous && segment === previous.segment && chunk > 0 && chunk <= previous.chunk) return current
  const base: StreamingState = previous ?? {
    run_id: next.run_id,
    root_run_id: next.root_run_id ?? streamSession,
    segment,
    chunk: 0,
    text: '',
    phase: 'content',
    updated_at: now,
  }
  if (next.type === 'stream_gap') {
    const gap: StreamingState = { ...base, segment, chunk, phase: 'gap', updated_at: now, frozen: true }
    return { ...current, [next.run_id]: gap }
  }
  const phase: StreamingState['phase'] = next.kind === 'tool_call_delta' ? 'tool' : next.kind === 'done' ? (next.tool_call_count ? 'tool' : 'done') : 'content'
  const updated: StreamingState = {
    ...base,
    root_run_id: next.root_run_id ?? base.root_run_id,
    segment,
    chunk,
    phase,
    tool_name: next.tool_name ?? base.tool_name,
    finish_reason: next.finish_reason ?? base.finish_reason,
    text: next.kind === 'content' ? base.text + (next.text ?? '') : base.text,
    updated_at: now,
    frozen: false,
  }
  return {
    ...current,
    [next.run_id]: updated,
  }
}

function reduceStreamBatch(
  current: Record<string, StreamingState>,
  items: StreamItem[],
  streamSession: string,
) {
  let next = current
  for (const item of items) next = reduceStreamItem(next, item, streamSession, Date.now())
  return next
}

type ConversationTurn = {
  id: string
  user?: EventItem
  process: EventItem[]
  /** Every ordinary assistant message remains visible in the transcript. */
  assistants: EventItem[]
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
const emptyScheduler: SchedulerState = { mode: 'idle', running: false, label: '等待启动' }
type Bootstrap = {
  project: { root: string } | null
  settings: SettingsState
  sessions: Session[]
  initial_prompt?: string | null
}
type CapabilityManifest = {
  version: string
  runtime: string
  transport: { commands: string; events: string; reconnect: string }
  features: Record<string, { supported?: boolean; reason?: string; [key: string]: unknown }>
  unsupported_controls?: Array<{ id?: string; label?: string; reason?: string }>
}
type Theme = 'dark' | 'light'
type CommandItem = {
  id: string
  command: string
  label: string
  description: string
  icon: LucideIcon
}

/** Drafts belong to a conversation, not to the global composer. */
function draftKeyForSession(runId: string | null | undefined, projectRoot: string | null | undefined) {
  return runId ? 'session:' + runId : 'new:' + (projectRoot ?? '')
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
  ApprovalRejected: '已拒绝本次动作，继续运行',
  RunResumed: '已继续运行',
  RunYielded: '运行暂存',
  RunOutcomeRecorded: '运行结果已记录',
  CompletionCandidateSubmitted: '完成条件已提交',
  CompletionAccepted: '完成条件已验证',
  RunStalled: '运行已暂停',
  RunBlocked: '任务已阻塞',
  RunFailed: '任务执行失败',
  RunCancelled: '运行已停止',
  ModelInvocationStarted: '模型调用中',
  ModelInvocationFinished: '模型调用完成',
  ModelInvocationFailed: '模型调用失败',
  TodoUpdated: '任务清单已更新',
  StallDiagnosisRecorded: '已记录暂停诊断',
  MemoryCandidateRecorded: '记忆候选已记录',
  MemoryCheckpointCommitted: '记忆检查点已保存',
  AssistantMessageInterrupted: '回复已中断并保存',
  ActionCancelled: '工具动作已取消',
}

const runtimeStatusLabels: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  started: '执行中',
  succeeded: '执行成功',
  completed: '已完成',
  failed: '执行失败',
  cancelled: '已停止',
  rejected: '已拒绝（未执行）',
  unknown: '结果待确认',
}

function projectDisplayName(root: string) {
  return root.split(/[\\/]/).filter(Boolean).pop() || root
}

function runtimeEventLabel(event: EventItem) {
  const eventLabel = runtimeEventLabels[event.event_type]
  if (eventLabel) return eventLabel
  // ToolObservationCommitted carries the durable transport status in its
  // payload, while the public event may expose a more precise presentation
  // status (for example, ``rejected`` means the approved tool never ran).
  // Prefer that projected status so the activity feed and tool card agree.
  if (event.status) {
    return runtimeStatusLabels[event.status] ?? statusLabels[event.status] ?? event.status
  }
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

function isTransientTransportError(reason: unknown) {
  const message = String((reason as { message?: unknown })?.message ?? reason ?? '').toLowerCase()
  return message === 'failed to fetch' || message.includes('networkerror') || message.includes('load failed')
}

function assistantHasToolCalls(event: EventItem) {
  const ids = event.payload?.tool_call_ids
  return Array.isArray(ids) && ids.some((id) => typeof id === 'string' && id.trim().length > 0)
}

/**
 * Convert the durable event stream into the compact turn shape used by the
 * DeepSeek Harness chat. Tool-driving assistant messages and tool calls are
 * retained as an expandable process section, while every ordinary assistant
 * message stays visible in the transcript. A model may commit multiple
 * ordinary messages for one user request; keeping only the last one silently
 * hid earlier answers in the collapsed thinking section.
 */
function groupConversationTurns(events: EventItem[]): ConversationTurn[] {
  const turns: ConversationTurn[] = []
  let current: ConversationTurn | null = null
  for (const event of events) {
    if (event.kind === 'user') {
      if (current) turns.push(current)
      current = { id: event.id, user: event, process: [], assistants: [] }
      continue
    }
    if (!current) {
      // A reconnect can begin in the middle of a run. Keep those process
      // events visible, but do not manufacture a user message for them.
      current = { id: 'orphan-' + event.id, process: [], assistants: [] }
    }
    if (event.kind === 'assistant' && normalizedUserContent(event.content)) {
      if (assistantHasToolCalls(event)) current.process.push(event)
      else current.assistants.push(event)
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
  let changed = false
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
    changed = true
  })
  if (!changed) return authoritative
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
    webApi<SettingsState>('/api/settings').then((value) => {
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
      const value = await webApi<SettingsState>('/api/settings?reveal=true')
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
      const result = await webApi<{ ok: boolean; message: string }>('/api/settings/test', {
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
      const result = await webApi<{ settings: SettingsState }>('/api/settings', {
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

type RuntimeDiagnosis = {
  tone: 'error' | 'warning' | 'info'
  title: string
  detail: string
  action?: string
  eventType?: string
  sequence?: number
}

function runtimeDiagnosisDetail(value: unknown) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return ''
  // Never put Python tracebacks, provider payloads, or raw protocol envelopes
  // into the browser. The durable event remains available through audit APIs.
  return text.replace(/Traceback \(most recent call last\):.*$/i, '').slice(0, 240).trim()
}

function deriveRuntimeDiagnosis(events: EventItem[], status: string, executor: ExecutorState): RuntimeDiagnosis | null {
  // Historical failure/diagnosis events remain visible in the activity feed,
  // but a successfully completed Run must not keep an old warning card pinned
  // in the current-state panel (especially when the optional sandbox is
  // degraded and the controlled native fallback was successful).
  if (status === 'completed' || status === 'cancelled') return null
  const candidates = events
    .filter((event) => [
      'StallDiagnosisRecorded', 'RunBlocked', 'RunStalled', 'RunFailed',
      'ModelInvocationFailed', 'ActionFailed', 'ActionOutcomeUnknown',
      'RunOutcomeRecorded',
    ].includes(event.event_type))
    .sort((left, right) => left.sequence - right.sequence)
  const event = [...candidates].reverse().find((item) => {
    if (item.event_type !== 'RunOutcomeRecorded') return true
    const outcome = item.outcome ?? item.payload
    return String(outcome?.outcome ?? '').toLowerCase() !== 'pass'
  })
  const raw = event?.event_type === 'RunOutcomeRecorded'
    ? (event.outcome?.details as Record<string, unknown> | undefined)?.reason ?? event.outcome?.reason ?? event.outcome?.primary_class
    : event?.payload?.diagnosis ?? event?.payload?.reason ?? event?.payload?.error_kind
  const detail = runtimeDiagnosisDetail(raw)

  if (executor.degraded && (!event || status === 'idle')) {
    return {
      tone: 'warning',
      title: '执行环境已降级',
      detail: 'OpenSandbox 当前不可用，Runtime 使用受控的本机后端继续工作。',
      action: '恢复沙箱后，新动作会重新使用沙箱；不会扩大既有权限。',
    }
  }
  if (!event && !['stalled', 'blocked', 'failed_recoverable', 'failed_terminal'].includes(status)) return null

  const normalized = detail.toLowerCase()
  if (/projection cursor|snapshot digest|event rebuild|投影/.test(normalized)) {
    return {
      tone: 'error',
      title: 'Runtime 状态投影需要重建',
      detail: '不可变事件流与派生投影暂时不一致，Runtime 已停止派发以保护任务状态。',
      action: '保留当前检查点后重建投影，再从原队列继续；不会重复执行未知副作用。',
      eventType: event?.event_type,
      sequence: event?.sequence,
    }
  }
  if (/completion candidate|verifiable completion|no progress|stalled|完成证据/.test(normalized)) {
    return {
      tone: 'warning',
      title: '任务暂时暂停',
      detail: '模型回复已经保存，但当前还没有满足目标合约的可验证完成证据。',
      action: '继续运行以补充验证；纯对话会在响应持久化后自动完成。',
      eventType: event?.event_type,
      sequence: event?.sequence,
    }
  }
  if (/outcome.?unknown|unknown outcome|结果待确认/.test(normalized)) {
    return {
      tone: 'error',
      title: '工具结果待确认',
      detail: '工具动作已经开始，但结果没有可靠落盘。Runtime 不会自动重放可能产生副作用的动作。',
      action: '先执行显式对账，再决定继续或重试。',
      eventType: event?.event_type,
      sequence: event?.sequence,
    }
  }
  if (/lease conflict|supervisor lease|租约/.test(normalized)) {
    return {
      tone: 'info',
      title: '另一个 Runtime 正在调度此项目',
      detail: '当前窗口仍可读取、发送和审批；任务由持有调度租约的进程执行。',
      action: '无需重复启动评测或 Runtime，等待现有调度器心跳/接管。',
      eventType: event?.event_type,
      sequence: event?.sequence,
    }
  }
  if (/api.?connection|provider|model.?request|rate.?limit|timeout|connection.?refused|连接失败|请求失败|模型/.test(normalized)) {
    return {
      tone: 'error',
      title: '模型请求失败',
      detail: '模型提供方请求没有成功返回，Runtime 已保留当前检查点和对话状态。',
      action: '检查 Base URL、API Key、模型名称和网络后，从当前检查点继续；不会丢失已落盘消息。',
      eventType: event?.event_type,
      sequence: event?.sequence,
    }
  }
  if (/sandbox|docker|environment_not_ready|环境/.test(normalized) || (executor.degraded && event)) {
    return {
      tone: 'warning',
      title: '执行环境不可用',
      detail: '沙箱或依赖服务没有就绪，任务状态已保留，未将环境故障伪装成任务成功。',
      action: '修复环境后从当前检查点继续。',
      eventType: event?.event_type,
      sequence: event?.sequence,
    }
  }
  const titleByEvent: Record<string, string> = {
    ModelInvocationFailed: '模型请求失败',
    ActionFailed: '工具执行失败',
    RunBlocked: '任务已阻塞',
    RunFailed: '任务执行失败',
    RunCancelled: '任务已停止',
  }
  return {
    tone: event?.event_type === 'ActionFailed' || event?.event_type === 'ModelInvocationFailed' ? 'error' : 'warning',
    title: (event && titleByEvent[event.event_type]) || (status === 'blocked' ? '任务已阻塞' : '运行需要处理'),
    detail: detail || 'Runtime 已保留当前事件和检查点，等待下一步处理。',
    action: status === 'stalled' || status === 'blocked' ? '可从当前检查点继续；如涉及未知副作用，请先对账。' : undefined,
    eventType: event?.event_type,
    sequence: event?.sequence,
  }
}

function RuntimeDiagnosisCard({ diagnosis }: { diagnosis: RuntimeDiagnosis }) {
  const Icon = diagnosis.tone === 'error' ? CircleAlert : diagnosis.tone === 'warning' ? AlertCircle : Info
  return (
    <section className={'inspector-card diagnosis-card ' + diagnosis.tone} aria-live="polite">
      <div className="diagnosis-heading"><span className="diagnosis-icon"><Icon size={16} /></span><div><span className="card-eyebrow">需要关注</span><strong>{diagnosis.title}</strong></div></div>
      <p>{diagnosis.detail}</p>
      {diagnosis.action && <div className="diagnosis-action"><span>下一步</span>{diagnosis.action}</div>}
      {diagnosis.eventType && <small className="diagnosis-source">来源：{runtimeEventLabels[diagnosis.eventType] ?? diagnosis.eventType}{diagnosis.sequence ? ' · 事件 #' + diagnosis.sequence : ''}</small>}
    </section>
  )
}

function toolStatusLabel(value: string | undefined) {
  if (!value) return '处理中'
  return runtimeStatusLabels[value] ?? statusLabels[value] ?? value
}

function TurnProcess({ events, live, onCopy }: { events: EventItem[]; live: boolean; onCopy: (text: string) => void }) {
  const [expanded, setExpanded] = useState(live)
  // Open the live process automatically, but do not force a user-collapsed
  // historical process back open on every SSE refresh.
  useEffect(() => {
    if (live) setExpanded(true)
  }, [live])
  if (events.length === 0) return null
  const toolCount = events.filter((event) => event.kind === 'tool').length
  const messageCount = events.filter((event) => event.kind === 'assistant').length
  const summary = [
    '已思考',
    toolCount > 0 ? `${toolCount} 次工具调用` : '',
    messageCount > 0 ? `${messageCount} 条过程消息` : '',
  ].filter(Boolean).join(' · ')
  return (
    <details className="turn-process" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary className="turn-process-summary">
        <span className="turn-process-chevron"><ChevronRight size={14} /></span>
        <span>{summary}</span>
        {live && <span className="turn-process-live">进行中</span>}
      </summary>
      {expanded && <div className="turn-process-body">
        {events.map((event) => <MessageCard event={event} key={event.id} onCopy={onCopy} />)}
      </div>}
    </details>
  )
}

const MessageCard = memo(function MessageCard({ event, onCopy }: { event: EventItem; onCopy: (text: string) => void }) {
  if (event.kind === 'user') {
    return <article className="message user-message"><div className="message-avatar user-avatar">你</div><div className="message-body"><div className="message-label">你</div><div className="message-content">{event.content}</div></div></article>
  }
  if (event.kind === 'assistant') {
    const streaming = Boolean(event.optimistic)
    const content = streaming
      ? <div className="message-content">{event.content}</div>
      : <div className="markdown message-content" dangerouslySetInnerHTML={renderMarkdown(event.content ?? '')} />
    return <article className={'message assistant-message ' + (streaming ? 'streaming-message' : '')}><div className="message-avatar assistant-avatar"><Sparkles size={15} /></div><div className="message-body"><div className="message-header"><div className="message-label">cc-harness</div>{event.content && !streaming && <button className="message-action" onClick={() => onCopy(event.content ?? '')} title="复制回复" aria-label="复制回复"><Copy size={14} /></button>}</div>{content}{streaming && <span className="streaming-cursor" aria-label="正在生成" />}</div></article>
  }
  if (event.kind === 'tool') {
    return <details className="tool-card"><summary><span className="tool-summary"><span className="tool-icon"><Play size={13} /></span><span className="tool-name">{event.tool_name || '工具调用'}</span><span className={'tool-status ' + (event.status === 'succeeded' ? 'ok' : event.status === 'failed' ? 'failed' : event.status === 'rejected' ? 'rejected' : '')}>{toolStatusLabel(event.status)}</span></span><ChevronDown size={15} /></summary><pre>{event.content || '（没有可展示的输出）'}</pre></details>
  }
  if (event.kind === 'error') {
    return <div className="event-notice error"><CircleAlert size={16} /><span>{friendlyError(event)}</span></div>
  }
  if (event.kind === 'outcome') {
    return <div className="event-notice outcome"><CircleCheck size={16} /><span>Runtime 已记录运行结果</span></div>
  }
  return <div className="event-notice"><span className="event-line" /><span>{runtimeEventLabel(event)}</span></div>
})

type PendingApproval = {
  approvalId: string
  digest: string
  actionId?: string
  scope: string[]
  /** Owning child Run, useful for diagnostics; commands stay root-scoped. */
  runId?: string
}

function ApprovalCard({ approval, onApprove, onReject, busy = false }: { approval: PendingApproval; onApprove: () => void; onReject: () => void; busy?: boolean }) {
  const action = approval.actionId || '需要授权的动作'
  return (
    <div className="approval-card" role="region" aria-label="待处理审批">
      <div className="approval-heading"><ShieldCheck size={17} /><span><strong>需要你的批准</strong><small>Runtime 正在等待这项本机操作</small></span></div>
      <div className="approval-detail"><span>动作</span><b>{action}</b>{approval.scope.length > 0 && <><span>范围</span><b className="approval-scope" title={approval.scope.join('\n')}>{approval.scope.join('、')}</b></>}</div>
      <div className="approval-actions"><button className="secondary-button" onClick={onReject} disabled={busy}>{busy ? '处理中…' : '拒绝'}</button><button className="primary-button" onClick={onApprove} disabled={busy}>{busy ? '处理中…' : '允许一次'}</button></div>
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
  const [streamingRuns, setStreamingRuns] = useState<Record<string, StreamingState>>({})
  const [context, setContext] = useState<ContextState>(emptyContext)
  const [executor, setExecutor] = useState<ExecutorState>(emptyExecutor)
  const [scheduler, setScheduler] = useState<SchedulerState>(emptyScheduler)
  const [webCapabilities, setWebCapabilities] = useState<CapabilityManifest | null>(null)
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([])
  const [approvalBusyId, setApprovalBusyId] = useState<string | null>(null)
  const [continuationNotice, setContinuationNotice] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [legacyEvents, setLegacyEvents] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [deletingSession, setDeletingSession] = useState<string | null>(null)
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
  // Project selection can read a large historical event store. Keep the
  // conversation switch responsive while that request is in flight and make
  // only the latest click allowed to update project/settings state.
  const selectionGeneration = useRef(0)
  const selectionAbort = useRef<AbortController | null>(null)
  const refreshTimer = useRef<number | null>(null)
  const transportRetry = useRef<{ runId: string; attempt: number }>({ runId: '', attempt: 0 })
  // A delete can race with an in-flight timeline/list request. Keep the
  // tombstoned run IDs out of every late response so an old snapshot cannot
  // make a successfully deleted conversation reappear in the sidebar.
  const hiddenSessionsRef = useRef<Set<string>>(new Set())
  const sessionEventsCacheRef = useRef<Record<string, EventItem[]>>({})
  const runtimeEventBatchRef = useRef<{ sessionId: string; events: EventItem[] } | null>(null)
  const runtimeEventFrameRef = useRef<number | null>(null)
  const streamBatchRef = useRef<{ sessionId: string; items: StreamItem[] } | null>(null)
  const streamFrameRef = useRef<number | null>(null)
  // A stream can arrive a frame after its durable assistant/terminal event.
  // Remember finalized run IDs so a late delta cannot resurrect a ghost
  // typing bubble after the committed answer is already visible.
  const finalizedStreamRunsRef = useRef<Set<string>>(new Set())
  const continuationGeneration = useRef(0)
  const conversationRef = useRef<HTMLElement | null>(null)
  const streamOpened = useRef(false)
  const stickToBottom = useRef(true)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const searchRef = useRef<HTMLInputElement | null>(null)
  // Keep an independent unsent draft for every conversation (and for the
  // current project's new-session composer). Refs make rapid session clicks
  // deterministic even while timeline/project requests are still in flight.
  const draftRef = useRef('')
  const draftsRef = useRef<Record<string, string>>({})
  const draftKeyRef = useRef(draftKeyForSession(null, project?.root))

  function visibleSessions(items: Session[]) {
    return items.filter((item) => !hiddenSessionsRef.current.has(item.run_id))
  }

  function rememberDraft() {
    draftsRef.current[draftKeyRef.current] = draftRef.current
  }

  function switchDraft(key: string, persist = true) {
    if (persist) rememberDraft()
    const value = draftsRef.current[key] ?? ''
    draftKeyRef.current = key
    draftRef.current = value
    setDraft(value)
    setCursorPosition(value.length)
  }

  function updateDraft(value: string) {
    draftRef.current = value
    draftsRef.current[draftKeyRef.current] = value
    setDraft(value)
  }

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { window.localStorage.setItem('cc-harness-theme', theme) } catch { /* localStorage may be unavailable */ }
  }, [theme])

  useEffect(() => {
    if (!toast) return undefined
    const timer = window.setTimeout(() => setToast(null), 3600)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    if (!continuationNotice) return undefined
    const timer = window.setTimeout(() => setContinuationNotice(null), 8000)
    return () => window.clearTimeout(timer)
  }, [continuationNotice])

  const refresh = useCallback(async () => {
    try {
      // Keep the first paint independent from the expensive all-project
      // history scan. The sidebar is filled as soon as its own request
      // completes, while the composer/project shell is usable immediately.
      const boot = await webApi<Bootstrap>('/api/bootstrap?include_sessions=false')
      // Capabilities are a server-owned contract. They let an upstream-style
      // control render as disabled with a reason instead of pretending that a
      // feature exists when this Runtime has not exposed it.
      const manifest = await webApi<CapabilityManifest>('/api/capabilities')
      setProject(boot.project)
      setWebCapabilities(manifest)
      setSettings({ ...boot.settings, permission_mode: normalizePermissionMode(boot.settings.permission_mode) })
      setSessions([])
      const initialSession = null
      const initialDraftKey = draftKeyForSession(initialSession, boot.project?.root)
      if (boot.initial_prompt && draftsRef.current[initialDraftKey] === undefined) {
        draftsRef.current[initialDraftKey] = boot.initial_prompt
      }
      if (draftKeyRef.current !== initialDraftKey) switchDraft(initialDraftKey, false)
      else if (!draftRef.current && boot.initial_prompt) updateDraft(boot.initial_prompt)
      setActiveSession((current) => current ?? initialSession)
      void webApi<{ sessions: Session[] }>('/api/sessions').then((list) => {
        const nextSessions = visibleSessions(list.sessions)
        setSessions(nextSessions)
        if (nextSessions[0]?.scheduler) setScheduler(nextSessions[0].scheduler)
        setActiveSession((current) => current ?? nextSessions[0]?.run_id ?? null)
      }).catch(() => {
        // The shell remains usable without the history scan; a later session
        // selection or send will retry the authoritative sidebar refresh.
      })
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const loadSession = useCallback(async (runId: string) => {
    if (hiddenSessionsRef.current.has(runId)) return
    const requestGeneration = ++loadGeneration.current
    try {
      // The timeline and run snapshot are needed to paint the selected
      // conversation. The all-project sidebar scan can be much slower (it
      // replays every project root), so fetch it after the conversation is
      // already visible instead of making a session switch wait for it.
      const [timeline, state] = await Promise.all([
        webApi<{ events: EventItem[] }>('/api/sessions/' + encodeURIComponent(runId) + '/timeline'),
        webApi<{ project_root?: string; context: ContextState; status: string; executor?: ExecutorState; scheduler?: SchedulerState; projection?: { approvals?: Array<{ approval_id: string; run_id?: string; action_args_digest: string; action_id?: string; scope?: string[]; status: string }> } }>('/api/sessions/' + encodeURIComponent(runId)),
      ])
      // A session can be switched while these two requests are in flight.
      // Never let a stale response replace the newly selected conversation.
      if (requestGeneration !== loadGeneration.current || activeSessionRef.current !== runId || hiddenSessionsRef.current.has(runId)) return
      transportRetry.current = { runId, attempt: 0 }
      // Union the snapshot with events that arrived through SSE while the
      // request was in flight; a slow timeline response must not erase a live
      // assistant/tool event.
      setEvents((current) => {
        const merged = mergeEventLists(current, timeline.events)
        sessionEventsCacheRef.current[runId] = merged
        return merged
      })
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
      setScheduler(state.scheduler ?? emptyScheduler)
      // ApprovalRequested remains immutable evidence even after a Run is
      // cancelled.  Only an awaiting_approval Run has an actionable card;
      // terminal snapshots must clear any card left from a prior render.
      setPendingApprovals(state.status === 'awaiting_approval'
        ? (state.projection?.approvals ?? []).filter((item) => item.status === 'requested').map((item) => ({ approvalId: item.approval_id, digest: item.action_args_digest, actionId: item.action_id, scope: item.scope ?? [], runId: item.run_id }))
        : [])
      setError(null)
      // Refresh the sidebar independently. A failure here must not blank the
      // selected transcript or turn a successful session load into an error.
      void webApi<{ sessions: Session[] }>('/api/sessions').then((list) => {
        if (requestGeneration === loadGeneration.current && activeSessionRef.current === runId && !hiddenSessionsRef.current.has(runId)) {
          setSessions(visibleSessions(list.sessions))
          const selected = list.sessions.find((item) => item.run_id === runId)
          if (selected?.scheduler) setScheduler(selected.scheduler)
        }
      }).catch(() => undefined)
    } catch (reason) {
      // A stale request may fail after the user has already switched sessions.
      // Keep that failure from surfacing over the currently selected session.
      if (requestGeneration !== loadGeneration.current || activeSessionRef.current !== runId || hiddenSessionsRef.current.has(runId)) return
      const apiReason = reason as Error & { status?: number; code?: string }
      if (isTransientTransportError(reason)) {
        const attempt = transportRetry.current.runId === runId ? transportRetry.current.attempt + 1 : 1
        transportRetry.current = { runId, attempt }
        if (attempt <= 5) {
          setError(null)
          setContinuationNotice('实时连接暂时中断，正在重新连接…')
          if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
          refreshTimer.current = window.setTimeout(() => {
            refreshTimer.current = null
            if (activeSessionRef.current === runId && loadGeneration.current === requestGeneration) void loadSession(runId)
          }, Math.min(2_000, 400 * attempt))
        } else {
          setError('暂时无法连接 Runtime，请检查服务是否仍在运行后重试。')
          setContinuationNotice(null)
        }
        return
      }
      if (apiReason.status === 404 || apiReason.code === 'session_not_found') {
        // A browser can retain a session id while another process cleans up
        // its project history.  Drop the stale selection instead of polling
        // the same 404 forever (the old UI produced a noisy stream of 400s).
        setActiveSession(null)
        setEvents([])
        setOptimisticEvents((current) => {
          const next = { ...current }
          delete next[runId]
          return next
        })
        setStreamingRuns({})
        setPendingApprovals([])
        setScheduler(emptyScheduler)
        setError('会话已不存在或已被其他窗口清理，请从左侧重新选择会话。')
        return
      }
      setError((reason as Error).message)
    } finally {
      // Only the latest selected session may clear its loading indicator. A
      // stale timeline/state response must not make a newer session look ready.
      if (requestGeneration === loadGeneration.current && activeSessionRef.current === runId) {
        setSessionLoading(false)
      }
    }
  }, [])

  const enqueueRuntimeEvent = useCallback((sessionId: string, event: EventItem) => {
    if (hiddenSessionsRef.current.has(sessionId)) return
    const current = runtimeEventBatchRef.current
    if (current?.sessionId === sessionId) current.events.push(event)
    else runtimeEventBatchRef.current = { sessionId, events: [event] }
    if (runtimeEventFrameRef.current !== null) return
    runtimeEventFrameRef.current = window.requestAnimationFrame(() => {
      runtimeEventFrameRef.current = null
      const pending = runtimeEventBatchRef.current
      runtimeEventBatchRef.current = null
      if (!pending || activeSessionRef.current !== pending.sessionId || hiddenSessionsRef.current.has(pending.sessionId)) return
      setEvents((events) => {
        const merged = mergeEventLists(events, pending.events)
        sessionEventsCacheRef.current[pending.sessionId] = merged
        return merged
      })
    })
  }, [])

  const enqueueStreamItem = useCallback((sessionId: string, next: StreamItem) => {
    if (!next.run_id || hiddenSessionsRef.current.has(sessionId) || finalizedStreamRunsRef.current.has(next.run_id)) return
    const current = streamBatchRef.current
    if (current?.sessionId === sessionId) current.items.push(next)
    else streamBatchRef.current = { sessionId, items: [next] }
    if (streamFrameRef.current !== null) return
    streamFrameRef.current = window.requestAnimationFrame(() => {
      streamFrameRef.current = null
      const pending = streamBatchRef.current
      streamBatchRef.current = null
      if (!pending || activeSessionRef.current !== pending.sessionId || hiddenSessionsRef.current.has(pending.sessionId)) return
      setStreamingRuns((currentRuns) => reduceStreamBatch(currentRuns, pending.items, pending.sessionId))
    })
  }, [])

  const scheduleSessionRefresh = useCallback((runId: string) => {
    if (activeSessionRef.current !== runId || hiddenSessionsRef.current.has(runId)) return
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null
      if (activeSessionRef.current === runId && !hiddenSessionsRef.current.has(runId)) void loadSession(runId)
    }, 120)
  }, [loadSession])

  const waitForApprovalContinuation = useCallback(async (runId: string) => {
    const generation = ++continuationGeneration.current
    const deadline = Date.now() + 15_000
    while (Date.now() < deadline) {
      await new Promise<void>((resolve) => window.setTimeout(resolve, 220))
      if (activeSessionRef.current !== runId || continuationGeneration.current !== generation) return
      try {
        const state = await webApi<{ status: string }>('/api/sessions/' + encodeURIComponent(runId))
        if (state.status !== 'awaiting_approval') {
          await loadSession(runId)
          if (activeSessionRef.current === runId && continuationGeneration.current === generation) {
            setContinuationNotice(['queued', 'running'].includes(state.status)
              ? '已拒绝本次动作，Runtime 正在继续执行'
              : '已拒绝本次动作；Runtime 已进入 ' + (statusLabels[state.status] ?? state.status))
          }
          return
        }
      } catch {
        // SSE and the next scheduled session refresh remain the source of
        // truth if this one status request races a WebUI reconnect.
      }
    }
    if (activeSessionRef.current === runId && continuationGeneration.current === generation) {
      await loadSession(runId)
      setContinuationNotice('拒绝已记录；Runtime 仍在恢复，请查看右侧实时状态')
    }
  }, [loadSession])

  function isStaleApprovalError(reason: unknown) {
    const apiReason = reason as { code?: string; status?: number; message?: string }
    const message = String(apiReason?.message ?? '')
    return apiReason?.code === 'approval_stale'
      || (apiReason?.status === 409 && apiReason?.code === 'approval_digest_mismatch')
      || message.includes('审批不存在、已处理')
  }

  async function refreshAfterStaleApproval(runId: string) {
    // The decision may already have been committed by another tab or by a
    // stop/cancellation boundary.  Clear the optimistic card immediately,
    // then let the durable snapshot decide whether a new approval remains.
    setPendingApprovals([])
    setContinuationNotice('审批已被处理，正在刷新会话状态…')
    await loadSession(runId)
    notify('审批状态已更新，已继续显示 Runtime 实际状态')
  }

  useEffect(() => {
    activeSessionRef.current = activeSession
    transportRetry.current = { runId: activeSession ?? '', attempt: 0 }
    loadGeneration.current += 1
    const cachedEvents = activeSession ? sessionEventsCacheRef.current[activeSession] : undefined
    setSessionLoading(Boolean(activeSession && cachedEvents === undefined))
    setEvents(cachedEvents ?? [])
    setStreamingRuns({})
    setPendingApprovals([])
    setApprovalBusyId(null)
    setContinuationNotice(null)
    continuationGeneration.current += 1
    setContext(emptyContext)
    setShowNewMessages(false)
    stickToBottom.current = true
    streamOpened.current = false
    if (!activeSession) {
      eventSource.current?.close()
      return undefined
    }
    const streamSession = activeSession
    void loadSession(streamSession)
    eventSource.current?.close()
    const source = new EventSource(webEventsUrl(streamSession, undefined, legacyEvents))
    eventSource.current = source
    source.onopen = () => { streamOpened.current = true }
    source.addEventListener('stream', (message) => {
      try {
        const next = JSON.parse((message as MessageEvent).data) as StreamItem
        if (!next.run_id) return
        enqueueStreamItem(streamSession, next)
      } catch {
        setError('实时流式数据格式不可用，已等待下一条消息。')
      }
    })
    source.addEventListener('runtime', (message) => {
      try {
        const next = JSON.parse((message as MessageEvent).data) as EventItem
        enqueueRuntimeEvent(streamSession, next)
        const terminalStreamEvent = ['AssistantMessageCommitted', 'RunCancelled', 'RunFailed'].includes(next.event_type)
        if (['AssistantMessageCommitted', 'RunOutcomeRecorded', 'RunStalled', 'RunCancelled', 'RunFailed'].includes(next.event_type)) {
          if (terminalStreamEvent) {
          finalizedStreamRunsRef.current.add(next.run_id)
          const pendingStream = streamBatchRef.current
          if (pendingStream?.sessionId === streamSession) {
            pendingStream.items = pendingStream.items.filter((item) => item.run_id !== next.run_id)
            if (pendingStream.items.length === 0) streamBatchRef.current = null
          }
          }
          setStreamingRuns((current) => {
            const nextState = { ...current }
            const runKey = next.run_id
            const state = nextState[runKey]
            if (next.event_type === 'AssistantMessageCommitted') {
              delete nextState[runKey]
            } else if (state) {
              nextState[runKey] = {
                ...state,
                phase: next.event_type === 'RunCancelled' ? 'stopped' : next.event_type === 'RunFailed' ? 'failed' : state.phase,
                updated_at: Date.now(),
                frozen: next.event_type !== 'AssistantMessageCommitted',
              }
            }
            return nextState
          })
        }
        setOptimisticEvents((current) => {
          const pending = removeAcknowledgedOptimistic([next], current[streamSession] ?? [])
          if (pending.length === 0) {
            if (!Object.prototype.hasOwnProperty.call(current, streamSession)) return current
            const nextState = { ...current }
            delete nextState[streamSession]
            return nextState
          }
          const previous = current[streamSession] ?? []
          if (pending.length === previous.length && pending.every((event, index) => event.id === previous[index]?.id)) return current
          return { ...current, [streamSession]: pending }
        })
        scheduleSessionRefresh(streamSession)
      } catch {
        setError('实时事件格式不可用，已等待下一条事件。')
      }
    })
    source.onerror = () => {
      if (!streamOpened.current && !legacyEvents) {
        // Older already-running WebUI processes do not know the versioned
        // path. Retry the same cursor through the preserved legacy endpoint;
        // this is transport fallback only and does not create a second state
        // store or execution path.
        setLegacyEvents(true)
        return
      }
      // EventSource retries with Last-Event-ID automatically. Mark the
      // transient stream as a gap and immediately reconcile the durable
      // projection so a disconnect never leaves the composer/status stale.
      setStreamingRuns((current) => {
        const next = { ...current }
        const candidate = Object.values(next).find((item) => item.root_run_id === streamSession || item.run_id === streamSession)
        if (candidate) next[candidate.run_id] = { ...candidate, phase: 'gap', frozen: true, updated_at: Date.now() }
        return next
      })
      scheduleSessionRefresh(streamSession)
    }
    return () => {
      source.close()
      if (runtimeEventFrameRef.current !== null) {
        window.cancelAnimationFrame(runtimeEventFrameRef.current)
        runtimeEventFrameRef.current = null
      }
      if (runtimeEventBatchRef.current?.sessionId === streamSession) runtimeEventBatchRef.current = null
      if (streamFrameRef.current !== null) {
        window.cancelAnimationFrame(streamFrameRef.current)
        streamFrameRef.current = null
      }
      if (streamBatchRef.current?.sessionId === streamSession) streamBatchRef.current = null
      if (refreshTimer.current !== null) {
        window.clearTimeout(refreshTimer.current)
        refreshTimer.current = null
      }
    }
  }, [activeSession, enqueueRuntimeEvent, enqueueStreamItem, legacyEvents, loadSession, scheduleSessionRefresh])

  const activeStreaming = useMemo(() => {
    if (!activeSession) return null
    const candidates = Object.values(streamingRuns).filter((item) => item.root_run_id === activeSession || item.run_id === activeSession)
    candidates.sort((left, right) => right.updated_at - left.updated_at)
    return candidates[0] ?? null
  }, [activeSession, streamingRuns])
  const streamingEvent = useMemo<EventItem | null>(() => {
    if (!activeStreaming || !activeStreaming.text) return null
    return {
      id: 'stream:' + activeStreaming.run_id + ':' + activeStreaming.segment,
      event_id: 'stream:' + activeStreaming.run_id + ':' + activeStreaming.chunk,
      run_id: activeStreaming.run_id,
      sequence: Number.MAX_SAFE_INTEGER,
      event_type: 'AssistantMessageStreaming',
      occurred_at: new Date(activeStreaming.updated_at).toISOString(),
      kind: 'assistant',
      content: activeStreaming.text,
      optimistic: true,
    }
  }, [activeStreaming])
  const displayEvents = useMemo(
    () => mergeEventLists(
      streamingEvent ? [...events, streamingEvent] : events,
      activeSession ? (optimisticEvents[activeSession] ?? []) : [],
    ),
    [activeSession, events, optimisticEvents, streamingEvent],
  )

  useEffect(() => {
    if (displayEvents.length === 0) return
    if (!stickToBottom.current) {
      setShowNewMessages(true)
      return
    }
    window.requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: activeStreaming ? 'auto' : 'smooth', block: 'end' }))
  }, [activeStreaming, displayEvents.length])

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
  const diagnosis = useMemo(
    () => deriveRuntimeDiagnosis(displayEvents, status, executor),
    [displayEvents, executor, status],
  )
  const connectionLabel = executor.degraded
    ? '沙箱不可用 · 已降级本机'
    : executor.backend === 'sandbox'
      ? 'OpenSandbox 正常'
      : executor.backend === 'native'
        ? '本机执行'
        : '等待 Runtime'
  const projectName = project?.root.split(/[\\/]/).filter(Boolean).pop() ?? '未选择项目'
  const unsupportedControls = useMemo(
    () => Object.entries(webCapabilities?.features ?? {})
      .filter(([, feature]) => feature.supported === false),
    [webCapabilities],
  )

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
    const generation = ++selectionGeneration.current
    const previousSession = activeSessionRef.current
    const previousDraftKey = draftKeyRef.current
    const sameProject = !session.project_root || project?.root === session.project_root
    setError(null)

    selectionAbort.current?.abort()
    selectionAbort.current = null

    // Switch the visible conversation before waiting for the project-control
    // request. This is especially important for historical sessions whose
    // project selection must scan a large event store.
    switchDraft(draftKeyForSession(session.run_id, session.project_root), true)
    setActiveSession(session.run_id)
    if (sameProject) return

    const controller = new AbortController()
    selectionAbort.current = controller
    try {
      const result = await webApi<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ path: session.project_root }), signal: controller.signal })
      if (selectionGeneration.current !== generation) return
      if (!result.selected) {
        switchDraft(previousDraftKey)
        if (activeSessionRef.current === session.run_id) setActiveSession(previousSession)
        return
      }
      setProject(result.project)
      if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
      setSessions(visibleSessions(result.sessions))
    } catch (reason) {
      if (controller.signal.aborted || selectionGeneration.current !== generation) return
      switchDraft(previousDraftKey)
      if (activeSessionRef.current === session.run_id) setActiveSession(previousSession)
      setError((reason as Error).message)
    } finally {
      if (selectionGeneration.current === generation) selectionAbort.current = null
    }
  }

  async function chooseProject() {
    try {
      const result = await webApi<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ picker: true }) })
      if (result.selected) {
        setProject(result.project)
        if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
        setSessions(visibleSessions(result.sessions))
        switchDraft(draftKeyForSession(null, result.project?.root))
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
      const result = await webApi<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ path }) })
      if (result.selected) {
        setProject(result.project)
        if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
        setSessions(visibleSessions(result.sessions))
        switchDraft(draftKeyForSession(null, result.project?.root))
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
      const result = await webApi<{ settings: SettingsState }>('/api/settings', {
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
    const text = draftRef.current.trim()
    if (!text || sending) return
    const requestedSession = activeSession
    // A stalled/cancelled run may be resumed in-place. Allow its next
    // generation to stream again instead of treating the old terminal event
    // as a permanent tombstone for the run ID.
    if (requestedSession) finalizedStreamRunsRef.current.clear()
    setSending(true)
    setError(null)
    setStreamingRuns({})
    try {
      const result = await webApi<{ session_id: string; sequence?: number; message_event_id?: string; continuation?: 'same_run' | 'child_run' }>('/api/messages', { method: 'POST', body: JSON.stringify({ text, session_id: requestedSession, project_root: project?.root ?? null }) })
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
      updateDraft('')
      setCursorPosition(0)
      // A first message materializes a new root Run. Move the composer key
      // from the project's new-session bucket to that Run so an unsent draft
      // typed after this response belongs to the conversation when the user
      // switches away and back.
      switchDraft(draftKeyForSession(sessionId, project?.root), false)
      setActiveSession(sessionId)
      // The command receipt is enough to unblock the composer. Refresh the
      // potentially expensive all-project sidebar scan independently so a
      // slow historical listing never makes a successful send feel stuck.
      void webApi<{ sessions: Session[] }>('/api/sessions').then((list) => {
        if (activeSessionRef.current === sessionId && !hiddenSessionsRef.current.has(sessionId)) {
          setSessions(visibleSessions(list.sessions))
        }
      }).catch(() => scheduleSessionRefresh(sessionId))
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSending(false)
    }
  }

  async function stopSession() {
    if (!activeSession) return
    try {
      await webApi('/api/sessions/' + encodeURIComponent(activeSession) + '/stop', { method: 'POST' })
      await loadSession(activeSession)
      const list = await webApi<{ sessions: Session[] }>('/api/sessions')
      setSessions(visibleSessions(list.sessions))
      notify('已请求停止当前运行')
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function deleteSession(session: Session) {
    if (deletingSession) return
    const confirmed = window.confirm(
      `从会话列表删除“${session.title}”？\n\n运行证据会保留在本地审计存储中，之后可由审计工具恢复查看。`,
    )
    if (!confirmed) return
    const runId = session.run_id
    // Invalidate any timeline/list request that is already in flight. Without
    // this guard an old response could arrive after DELETE and put the
    // conversation back into the sidebar.
    hiddenSessionsRef.current.add(runId)
    loadGeneration.current += 1
    setDeletingSession(runId)
    setError(null)
    const finalizeDeletedSession = () => {
      // Keep the tombstone in hiddenSessionsRef permanently for this browser
      // lifetime; audit facts remain durable, but stale snapshots must never
      // resurrect the conversation after a successful delete.
      loadGeneration.current += 1
      setSessions((current) => current.filter((item) => item.run_id !== runId))
      setOptimisticEvents((current) => {
        const next = { ...current }
        delete next[runId]
        return next
      })
      setStreamingRuns((current) => {
        const next = { ...current }
        delete next[runId]
        return next
      })
      rememberDraft()
      delete draftsRef.current[draftKeyForSession(runId, session.project_root)]
      delete sessionEventsCacheRef.current[runId]
      // Keep the new-session draft for this project available when the user
      // starts another conversation after deleting the active one.
      if (activeSessionRef.current === runId) {
        eventSource.current?.close()
        eventSource.current = null
        activeSessionRef.current = null
        setActiveSession(null)
        setEvents([])
        setPendingApprovals([])
        setContext(emptyContext)
        setExecutor(emptyExecutor)
        setScheduler(emptyScheduler)
        switchDraft(draftKeyForSession(null, project?.root), false)
      }
    }
    try {
      await webApi<{ deleted: boolean }>('/api/sessions/' + encodeURIComponent(runId), { method: 'DELETE' })
      finalizeDeletedSession()
      notify('会话已从列表删除（审计记录已保留）')
    } catch (reason) {
      const apiReason = reason as Error & { status?: number; code?: string }
      // Another tab may have completed the tombstone first. Treat that as an
      // idempotent success so the stale browser card is removed as well.
      if (apiReason.status === 404 || apiReason.code === 'session_not_found') {
        finalizeDeletedSession()
        notify('会话已删除（审计记录已保留）')
        return
      }
      hiddenSessionsRef.current.delete(runId)
      setError(apiReason.message || '删除会话失败，请稍后重试')
      // The request may have invalidated the selected session's load. Restore
      // the authoritative view after a recoverable delete conflict.
      if (activeSessionRef.current === runId) void loadSession(runId)
    } finally {
      setDeletingSession(null)
    }
  }

  async function resumeSession() {
    if (!activeSession) return
    finalizedStreamRunsRef.current.clear()
    try {
      await webApi('/api/sessions/' + encodeURIComponent(activeSession) + '/resume', { method: 'POST' })
      await loadSession(activeSession)
      notify('已从最近检查点继续')
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function approve(approval: { approvalId: string; digest: string }) {
    if (!activeSession) return
    const runId = activeSession
    setApprovalBusyId(approval.approvalId)
    setContinuationNotice('已提交允许，Runtime 正在继续…')
    try {
      await webApi('/api/sessions/' + encodeURIComponent(runId) + '/approvals/' + encodeURIComponent(approval.approvalId) + '/approve', { method: 'POST', body: JSON.stringify({ action_args_digest: approval.digest }) })
      await loadSession(runId)
      notify('动作已批准，Runtime 正在继续')
      void waitForApprovalContinuation(runId)
    } catch (reason) {
      if (isStaleApprovalError(reason)) {
        await refreshAfterStaleApproval(runId)
      } else {
        setError((reason as Error).message)
        setContinuationNotice(null)
        // Keep the durable state visible after an unexpected control-plane
        // error; a later poll/SSE event can still reconcile the card.
        void loadSession(runId)
      }
    } finally {
      setApprovalBusyId(null)
    }
  }

  async function reject(approval: PendingApproval) {
    if (!activeSession) return
    const runId = activeSession
    setApprovalBusyId(approval.approvalId)
    setContinuationNotice('已提交拒绝，Runtime 正在继续…')
    try {
      await webApi('/api/sessions/' + encodeURIComponent(runId) + '/approvals/' + encodeURIComponent(approval.approvalId) + '/reject', { method: 'POST', body: JSON.stringify({ reason: 'WebUI 用户拒绝' }) })
      await loadSession(runId)
      notify('已拒绝本次动作，Runtime 正在继续')
      void waitForApprovalContinuation(runId)
    } catch (reason) {
      if (isStaleApprovalError(reason)) {
        await refreshAfterStaleApproval(runId)
      } else {
        setError((reason as Error).message)
        setContinuationNotice(null)
        // See approve(): refresh the authoritative snapshot after an
        // unexpected decision error and clear terminal approvals from the
        // composer when the durable state permits it.
        void loadSession(runId)
      }
    } finally {
      setApprovalBusyId(null)
    }
  }

  const copyText = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      notify('已复制到剪贴板')
    } catch {
      notify('当前环境不允许访问剪贴板')
    }
  }, [])

  function selectCommand(item: CommandItem) {
    updateDraft('')
    setCursorPosition(0)
    setCommandIndex(0)
    if (item.id === 'new') {
      setActiveSession(null)
      setEvents([])
      setStreamingRuns({})
      setOptimisticEvents({})
      setPendingApprovals([])
      setContext(emptyContext)
      setScheduler(emptyScheduler)
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
      setStreamingRuns({})
      setContext(emptyContext)
      notify('已清空当前视图；Durable Runtime 事件仍可审计')
    } else if (item.id === 'help') {
      notify('可用命令：/new、/project、/status、/resume、/settings、/clear')
    }
  }

  function startNewSession() {
    switchDraft(draftKeyForSession(null, project?.root))
    setActiveSession(null)
    setEvents([])
    setStreamingRuns({})
    setOptimisticEvents({})
    setPendingApprovals([])
    setContext(emptyContext)
    setScheduler(emptyScheduler)
    setContinuationNotice(null)
    setError(null)
    notify('已准备新会话')
  }

  function focusSidebarSearch() {
    // The reference client expands its collapsed search affordance in place.
    // Restore the rail first, then focus after React has committed the field.
    setLeftCollapsed(false)
    window.requestAnimationFrame(() => searchRef.current?.focus())
  }

  function usePrompt(prompt: string) {
    updateDraft(prompt)
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
        updateDraft('')
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

  // A fresh workspace follows the DeepSeek Harness hero composition: there
  // is no conversation chrome to compete with the first action, so the
  // welcome copy and composer can share one centered stage. Active runs keep
  // the normal top bar, scrollable transcript, and bottom status bar.
  const emptyState = !sessionLoading && !activeSession && conversationTurns.length === 0
  const shellClass = ['app-shell', leftCollapsed ? 'left-collapsed' : '', rightCollapsed ? 'right-collapsed' : ''].filter(Boolean).join(' ')
  const mainClass = ['main-panel', emptyState ? 'empty-state-panel' : '', emptyState && !project ? 'empty-no-project' : ''].filter(Boolean).join(' ')
  return (
    <div className={shellClass}>
      <aside className="left-sidebar">
        <div className="brand-row">
          <div className="brand-mark"><img src="/mascot.png" alt="" /></div>
          <span className="brand-name">cc-harness</span>
          <span className="brand-live" title="本地 WebUI 已连接" />
          <button className="icon-button sidebar-menu" onClick={() => setLeftCollapsed(true)} title="收起侧栏"><PanelLeftClose size={16} /></button>
        </div>
        <div className="sidebar-rail-tools" aria-label="侧栏快捷操作">
          <button className="rail-tool rail-expand" onClick={() => setLeftCollapsed(false)} aria-label="展开侧栏" title="展开侧栏"><PanelLeftOpen size={17} /></button>
          <button className="rail-tool" onClick={startNewSession} aria-label="新建会话" title="新建会话"><MessageSquarePlus size={17} /></button>
          <button className="rail-tool" onClick={chooseProject} aria-label="选择工作区" title="选择工作区"><FolderOpen size={17} /></button>
          <button className="rail-tool" onClick={focusSidebarSearch} aria-label="搜索会话" title="搜索会话"><Search size={17} /></button>
        </div>
        <button className="new-session-button" onClick={startNewSession} aria-label="新建会话"><MessageSquarePlus size={16} /><span>新会话</span></button>
        <button className="workspace-switcher" onClick={chooseProject}>
          <span className="workspace-icon"><Folder size={15} /></span>
          <span className="workspace-copy"><small>当前项目</small><strong>{projectName}</strong></span>
          <ChevronDown size={15} />
        </button>
        <div className="session-heading"><div><span>工作区</span><small>{sessions.length ? sessions.length + ' 个会话' : '暂无会话'}</small></div><div className="session-heading-actions"><button className="icon-button" onClick={focusSidebarSearch} title="搜索会话" aria-label="搜索会话"><Search size={16} /></button><button className="icon-button" onClick={startNewSession} title="新会话" aria-label="新建会话"><MessageSquarePlus size={17} /></button></div></div>
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
                  : groupSessions.map((session) => <div className="session-item-row" key={session.run_id}><button className={'session-item ' + (activeSession === session.run_id ? 'selected' : '')} onClick={() => void activateSession(session)}><StatusDot status={session.status} /><span className="session-title">{session.title}</span><span className="session-status">{statusLabels[session.status] ?? session.status}<span className="session-sequence"> · {session.sequence} 事件</span></span></button><button type="button" className="session-delete" onClick={(event) => { event.stopPropagation(); void deleteSession(session) }} disabled={deletingSession === session.run_id} aria-label={'删除会话 ' + session.title} title="删除会话">{deletingSession === session.run_id ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}</button></div>)}
              </div>}
            </section>
          })}
        </div>
        <div className="sidebar-bottom">
          <button className="project-chip" onClick={chooseProject}><FolderOpen size={15} /><span>{project ? project.root : '选择项目文件夹'}</span><ChevronRight size={14} /></button>
          <div className="sidebar-actions"><button className="settings-button" onClick={() => setSettingsOpen(true)} aria-label="设置"><Settings size={17} /><span>设置</span></button><button className="collapse-button" onClick={() => setLeftCollapsed(true)} title="收起侧栏" aria-label="收起侧栏"><PanelLeftClose size={16} /></button></div>
        </div>
      </aside>
      {leftCollapsed && <button className="expand-sidebar" onClick={() => setLeftCollapsed(false)} title="展开侧栏" aria-label="展开侧栏"><PanelLeftOpen size={18} /></button>}

      <main className={mainClass}>
        <header className="topbar">
          <div className="breadcrumb"><span className="topbar-project">{projectName}</span><ChevronRight size={14} /><span className="topbar-title">{sessionLoading ? '正在载入会话…' : active ? active.title : '新会话'}</span></div>
          <div className="topbar-actions"><span className="topbar-live"><span className="pulse-dot" />本地 Runtime</span><button className="topbar-icon" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')} title={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}>{theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}</button><button className="topbar-icon" onClick={() => setRightCollapsed((value) => !value)} title={rightCollapsed ? '打开运行面板' : '收起运行面板'}>{rightCollapsed ? <PanelRightOpen size={17} /> : <PanelRightClose size={17} />}</button><button className="topbar-icon" onClick={() => setSettingsOpen(true)} title="设置"><Settings size={16} /></button></div>
        </header>
              <section ref={conversationRef} className="conversation" aria-live="polite" onScroll={handleConversationScroll}>
            <div className="conversation-inner">
              {!project && <div className="welcome-state no-project"><div className="welcome-orbit"><Sparkles size={25} /></div><span className="welcome-kicker">LOCAL AGENT WORKSPACE</span><div className="welcome-title-line"><h1>把你的项目交给 cc-harness</h1><span className="preview-badge">预览版</span></div><p>先选择一个本机项目文件夹，主 Agent 才能在安全边界内读取和修改代码。<br />浏览器关闭不会停止已提交的 Durable Run。</p><div className="welcome-actions"><button className="primary-button" onClick={chooseProject}><FolderOpen size={16} />选择项目文件夹</button><button className="text-button" onClick={chooseProjectManually}>手动输入路径</button></div><div className="welcome-note"><ShieldCheck size={14} />本地处理 · 可恢复检查点 · 可审计事件</div></div>}
              {project && sessionLoading && <div className="session-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={22} /><div><strong>正在载入会话</strong><span>正在从 Durable Runtime 对账事件、状态和检查点…</span></div></div>}
              {project && !sessionLoading && conversationTurns.length === 0 && <div className="welcome-state project-ready"><div className="welcome-orbit small"><Bot size={23} /></div><span className="welcome-kicker">项目已就绪 · {projectName}</span><div className="welcome-title-line"><h1>今天要完成什么？</h1><span className="preview-badge">预览版</span></div><p>描述目标，cc-harness 会先读取项目状态，再由 Durable Runtime 编排执行。</p></div>}
              <div className="message-stack">{conversationTurns.map((turn, index) => <section className="conversation-turn" key={turn.id}>{turn.user && <MessageCard event={turn.user} onCopy={copyText} />}<TurnProcess events={turn.process} live={active != null && ['running', 'awaiting_approval'].includes(active.status) && index === conversationTurns.length - 1} onCopy={copyText} />{turn.assistants.map((event) => <MessageCard event={event} key={event.id} onCopy={copyText} />)}</section>)}{active && ['running', 'queued', 'awaiting_approval'].includes(active.status) && <div className="typing-indicator"><span /><span /><span /><em>{activeStreaming?.phase === 'tool' ? '正在准备工具调用' : activeStreaming?.phase === 'done' ? '正在整理回复' : activeStreaming?.phase === 'gap' ? '实时流暂时中断，正在从 Runtime 对账' : 'Runtime 正在工作'}</em></div>}{activeStreaming?.phase === 'stopped' && activeStreaming.text && <div className="event-notice"><CircleStop size={15} /><span>已停止，保留已生成的内容</span></div>}{activeStreaming?.phase === 'failed' && activeStreaming.text && <div className="event-notice error"><CircleAlert size={15} /><span>本轮失败，保留已生成的内容</span></div>}{continuationNotice && <div className="continuation-notice" role="status"><LoaderCircle size={14} className="spin" /><span>{continuationNotice}</span></div>}{pendingApprovals.length > 0 && <ApprovalCard approval={pendingApprovals[0]} busy={approvalBusyId === pendingApprovals[0].approvalId} onApprove={() => void approve(pendingApprovals[0])} onReject={() => void reject(pendingApprovals[0])} />}<div ref={bottomRef} /></div>
            </div>
            {showNewMessages && <button className="jump-to-latest" onClick={jumpToLatest}><ChevronDown size={14} />跳到最新消息</button>}
          </section>
        {error && <div className="error-banner"><AlertCircle size={16} /><span>{error}</span><button onClick={() => setError(null)} aria-label="关闭错误"><X size={14} /></button></div>}

        <footer className="composer-wrap">
          <div className="composer-label-row"><span>{sessionLoading ? '正在载入会话…' : active ? '继续与 cc-harness 协作' : '新的任务'}</span><span className="composer-shortcut"><Keyboard size={13} /> Enter 发送 · Shift+Enter 换行</span></div>
          <div className="composer">
            {commandPaletteOpen && <CommandPalette items={visibleCommands} selectedIndex={commandIndex} onSelect={selectCommand} />}
            <div className="composer-toolbar"><button className={'composer-project ' + (!project ? 'needs-project' : '')} onClick={chooseProject}><FolderOpen size={16} /><span>{project ? projectName : '选择项目文件夹'}</span><ChevronDown size={14} /></button><div className="composer-toolbar-right"><span className="composer-mode"><ShieldCheck size={13} />本地 Runtime</span></div></div>
             <textarea ref={textareaRef} value={draft} onChange={(event) => { updateDraft(event.target.value); setCursorPosition(event.currentTarget.selectionStart ?? event.currentTarget.value.length); setCommandIndex(0) }} onKeyDown={handleComposerKeyDown} onClick={(event) => { syncCursorPosition(event); if (!project) notify('请先选择本地项目文件夹') }} onKeyUp={syncCursorPosition} onSelect={syncCursorPosition} disabled={!project || sending || sessionLoading} placeholder={sessionLoading ? '正在载入会话…' : project ? '描述要完成的任务，或输入 / 查看命令…' : '请先选择本地项目文件夹'} rows={3} aria-label="任务输入框" />
           <div className="composer-footer"><div className="composer-footer-left"><PermissionSelector mode={settings.permission_mode} onChange={(mode) => void changePermissionMode(mode)} /><span className="privacy-note"><ShieldCheck size={13} />内容只在本机 Runtime 处理</span></div><div className="composer-actions">{active && ['running', 'queued', 'awaiting_approval'].includes(active.status) && <button className="stop-button" onClick={() => void stopSession()}><CircleStop size={15} />停止</button>}{active && ['cancelled', 'stalled', 'failed_recoverable', 'blocked'].includes(active.status) && <button className="secondary-button compact" onClick={() => void resumeSession()}><RotateCcw size={15} />{active.status === 'blocked' ? '确认继续' : '继续'}</button>}<button className="send-button" onClick={() => void sendMessage()} disabled={!project || !draft.trim() || sending || sessionLoading} aria-label="发送">{sending ? <LoaderCircle size={16} className="spin" /> : <Send size={16} />}<span>{sending ? '提交中' : '发送'}</span></button></div></div>
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
           <section className="inspector-card runtime-overview"><div className="card-eyebrow"><span className="pulse-dot" /> DURABLE RUNTIME</div><div className="runtime-state"><StatusDot status={status} /><div><strong>{status === 'idle' ? '等待输入' : statusLabels[status] ?? status}</strong><span>{active ? '事件序号 ' + active.sequence : '选择项目后开始'}</span></div></div><div className="inspector-row"><span>项目</span><strong>{project ? '已选择' : '未选择'}</strong></div><div className="inspector-row"><span>活动会话</span><strong>{active ? '已连接' : '—'}</strong></div><div className="inspector-row"><span>执行后端</span><strong className={executor.degraded ? 'warn-text' : ''}>{connectionLabel}</strong></div><div className="inspector-row"><span>调度器</span><strong className={scheduler.mode === 'external' ? 'warn-text' : ''}>{scheduler.label}</strong></div><div className="inspector-row"><span>审批</span><strong className={pendingApprovals.length > 0 ? 'warn-text' : ''}>{pendingApprovals.length > 0 ? pendingApprovals.length + ' 项待处理' : '无待处理'}</strong></div></section>
           {unsupportedControls.length > 0 && <section className="inspector-card capability-note"><div className="card-eyebrow">当前运行时能力</div><p>以下控制由服务端标记为不可用：</p>{unsupportedControls.map(([id, feature]) => <div className="capability-row" key={id}><strong>{id}</strong><span>{feature.reason ?? '当前环境未提供'}</span></div>)}</section>}
           {diagnosis && <RuntimeDiagnosisCard diagnosis={diagnosis} />}
           <section className="inspector-card live-status-card"><div className="card-heading"><div><span className="card-eyebrow">实时状态</span><small>{active ? active.title : '选择会话后显示'}</small></div><StatusDot status={status} /></div><div className="live-status-summary"><StatusDot status={status} /><div><strong>{status === 'idle' ? '等待输入' : statusLabels[status] ?? status}</strong><span>{active ? '事件序号 ' + active.sequence : '当前没有活动会话'}</span></div></div>{liveStatusEvents.length === 0 ? <div className="activity-empty"><Info size={15} /><span>任务运行后，这里会显示实时状态。</span></div> : <div className="runtime-timeline">{liveStatusEvents.map((event) => <div className={'runtime-timeline-row ' + event.kind} key={event.id}><span className="runtime-timeline-mark" /><div><strong>{runtimeEventLabel(event)}</strong><small>事件 #{event.sequence}</small></div></div>)}</div>}</section>
        </div>
        <div className="inspector-model"><div className="model-chip"><span className="model-chip-dot" /><div><small>当前模型</small><strong>{settings.model || '未配置'}</strong></div></div><button className="icon-button" onClick={() => setSettingsOpen(true)} title="设置"><Settings size={16} /></button></div>
      </aside>
      {settingsOpen && <SettingsModal initial={settings} onClose={() => setSettingsOpen(false)} onSaved={(value) => setSettings(value)} />}
      {toast && <div className="toast" role="status"><Check size={15} />{toast}</div>}
    </div>
  )
}

export { App }

