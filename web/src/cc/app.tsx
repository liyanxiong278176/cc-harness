import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import type { LucideIcon } from 'lucide-react'
import {
  AlertCircle, Bot, Check, ChevronDown, ChevronRight, ExternalLink, File, FileCode2, FileText,
  CircleAlert, CircleCheck, CircleStop, Command, Copy, Eye, EyeOff,
  Folder, FolderOpen, History, Info, Keyboard, LoaderCircle,
  Maximize2, MessageSquarePlus, Minimize2, Moon, PanelLeftClose, PanelLeftOpen,
  PanelRightClose, PanelRightOpen, Play, RefreshCw, RotateCcw, Search,
  Send, Settings, ShieldCheck, Sparkles, Sun, Trash2, X,
} from 'lucide-react'
import { webApi, webEventsUrl, type ApiError, type StreamErrorDetail } from './api'
import { StreamingMessage } from '../components/chat/StreamingMessage'
import { WelcomeSuggestions } from '../components/chat/WelcomeSuggestions'
import { SyntaxCode } from '../components/right-panel/SyntaxCode'
import { reduceStreamBatch, type StreamItem, type StreamingState } from '../state/streaming'
// The conversation surface and Runtime projection remain separate from the
// shared visual system in the Vite entry stylesheet.

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
const SSE_STALE_RETRY_THRESHOLD = 5
const SSE_COPY = {
  reconnecting: '实时连接暂时中断，正在重连…',
  reconnectingAfterDisconnect: '实时连接中断，正在重连；任务仍在后台运行',
  stale: '实时连接多次中断，当前数据可能滞后；任务仍在后台运行',
  staleBadge: '实时数据可能滞后',
  connectingBadge: '实时连接中…',
  reconcileError: '实时连接暂时无法读取运行状态，任务仍在后台继续',
} as const
type Session = {
  run_id: string
  title: string
  status: string
  sequence: number
  created_at?: string | null
  updated_at?: string | null
  active_worker_id?: string | null
  project_root?: string
  scheduler?: SchedulerState
}
type QueueProjection = {
  follow_up_run_id: string
  predecessor_run_id?: string | null
  message_artifact?: string
  gate?: string
  status?: string
  queued_sequence?: number
}
type QueuedMessage = QueueProjection & {
  /** The artifact digest is intentionally not shown; text comes from the public event. */
  content: string
  optimistic?: boolean
}
type GoalContractView = {
  objective: string
  acceptance_criteria: string[]
  constraints: string[]
  allowed_scope: string[]
  excluded_scope: string[]
  required_evidence: string[]
  human_review: string[]
  contract_version?: number
  interaction_mode?: string
}
type PlanNodeView = {
  node_id: string
  kind: string
  depth: number
  depends_on: string[]
  owned_paths: string[]
  child_run_id?: string | null
  worktree_id?: string | null
  required?: boolean
  effect_class?: string
  acceptance_criteria: string[]
  timeout_seconds?: number
  max_retries?: number
}
type PlanGraphView = {
  nodes: PlanNodeView[]
  revision?: number
  max_concurrent_children?: number
  max_child_depth?: number
}
type TodoView = {
  todo_id: string
  title: string
  status: string
  active_sessions: string[]
  updated_sequence?: number
  evidence_count: number
}
type RunFacts = {
  goal: GoalContractView | null
  plan: PlanGraphView | null
  todos: TodoView[]
  discovery_status?: string
  mutation_gate?: string
}
const emptyRunFacts: RunFacts = { goal: null, plan: null, todos: [] }
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
  tool_arguments?: Record<string, unknown>
  status?: string
  read_paths?: string[]
  modified_paths?: string[]
  result_lines_total?: number
  result_lines_shown?: number
  result_lines_unit?: string
  result_truncated?: boolean
  complete?: boolean
  next_cursor?: string
  observation_id?: string
  exit_code?: number
  duration_ms?: number
  error?: StreamErrorDetail
  payload?: Record<string, unknown>
  outcome?: Record<string, unknown>
  optimistic?: boolean
  /** Public event id returned with a command receipt, used for reconciliation. */
  ack_event_id?: string
  /** Browser-only snapshot; it is never included in a Runtime event. */
  stream_state?: StreamingState
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
 * cc-harness conversation. Tool-driving assistant messages and tool calls are
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

function sessionTimeGroup(session: Session, now = new Date()): 'today' | 'week' | 'older' {
  const updated = Date.parse(session.updated_at ?? session.created_at ?? '')
  if (!Number.isFinite(updated)) return 'older'
  const activity = new Date(updated)
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const activityDay = new Date(activity.getFullYear(), activity.getMonth(), activity.getDate()).getTime()
  if (activityDay >= today) return 'today'
  if (today - activityDay < 7 * 24 * 60 * 60 * 1000) return 'week'
  return 'older'
}

/**
 * Build the visible follow-up queue from public, durable events.  The browser
 * never treats a local timer or an optimistic counter as the source of truth:
 * a queued message is visible only after the Runtime has emitted
 * ``FollowUpQueued``.  ``FollowUpStarted`` and terminal child events remove it
 * from the pending list without touching the immutable transcript.
 */
function deriveQueuedMessages(events: EventItem[]): QueuedMessage[] {
  const queue = new Map<string, QueuedMessage>()
  const ordered = [...events].sort((left, right) => {
    const time = eventTime(left) - eventTime(right)
    return time || left.sequence - right.sequence
  })
  for (const event of ordered) {
    const payload = event.payload ?? {}
    if (event.event_type === 'FollowUpQueued') {
      const followUpId = String(payload.follow_up_run_id ?? '')
      if (!followUpId) continue
      queue.set(followUpId, {
        follow_up_run_id: followUpId,
        predecessor_run_id: payload.predecessor_run_id ? String(payload.predecessor_run_id) : null,
        message_artifact: payload.message_artifact ? String(payload.message_artifact) : undefined,
        gate: payload.gate ? String(payload.gate) : 'waiting',
        status: 'queued',
        queued_sequence: event.sequence,
        content: normalizedUserContent(event.content) || '已排队的后续消息',
      })
      continue
    }
    if (event.event_type === 'FollowUpStarted') {
      const followUpId = String(payload.follow_up_run_id ?? '')
      const item = queue.get(followUpId)
      if (item) queue.set(followUpId, { ...item, status: 'started' })
      continue
    }
    if (event.event_type === 'PredecessorBypassed') {
      const targetId = String(payload.follow_up_run_id ?? '')
      const item = queue.get(targetId)
      if (item) queue.set(targetId, { ...item, gate: 'bypassed' })
      continue
    }
    // A child Run can finish without a corresponding root queue update.  It is
    // safe to hide that queue row because the child transcript remains
    // rendered from its own immutable events.
    if (['RunOutcomeRecorded', 'RunCancelled', 'RunFailed'].includes(event.event_type)) {
      queue.delete(event.run_id)
    }
  }
  return [...queue.values()]
    .filter((item) => item.status === 'queued')
    .sort((left, right) => (left.queued_sequence ?? 0) - (right.queued_sequence ?? 0))
}

type RuntimeStage = 'context' | 'stream' | 'tool' | 'approval' | 'verify' | 'done'
const runtimeStageLabels: Record<RuntimeStage, string> = {
  context: '准备上下文',
  stream: '流式回复',
  tool: '工具调用',
  approval: '等待审批',
  verify: '验证结果',
  done: '已完成',
}
const runtimeStages: RuntimeStage[] = ['context', 'stream', 'tool', 'approval', 'verify']

function currentRuntimeStage(status: string, streaming: StreamingState | null, events: EventItem[], hasApproval: boolean): RuntimeStage {
  if (status === 'completed') return 'done'
  if (hasApproval || status === 'awaiting_approval') return 'approval'
  if (['stalled', 'blocked', 'failed_recoverable', 'failed_terminal', 'cancel_requested'].includes(status)) return 'verify'
  const latestSequence = events.length > 0 ? events[events.length - 1].sequence : 0
  if (streaming?.phase === 'tool' || events.some((event) => event.event_type === 'ActionStarted' && event.sequence >= Math.max(0, latestSequence) - 3)) return 'tool'
  if (streaming?.phase === 'done' || events.some((event) => event.event_type === 'CompletionCandidateSubmitted')) return 'verify'
  if (status === 'running') return 'stream'
  return 'context'
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

type UnknownRecord = Record<string, unknown>

function recordOf(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null
}

function stringList(value: unknown) {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => typeof item === 'string' ? item.trim() : '')
    .filter(Boolean)
}

function numberOrUndefined(value: unknown) {
  if (value === null || value === undefined || value === '') return undefined
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) ? number : undefined
}

function parseGoalContract(value: unknown): GoalContractView | null {
  const record = recordOf(value)
  if (!record) return null
  const objective = typeof record.objective === 'string' ? record.objective.trim() : ''
  const acceptanceCriteria = stringList(record.acceptance_criteria)
  if (!objective || acceptanceCriteria.length === 0) return null
  return {
    objective,
    acceptance_criteria: acceptanceCriteria,
    constraints: stringList(record.constraints),
    allowed_scope: stringList(record.allowed_scope),
    excluded_scope: stringList(record.excluded_scope),
    required_evidence: stringList(record.required_evidence),
    human_review: stringList(record.human_review),
    contract_version: numberOrUndefined(record.contract_version),
    interaction_mode: typeof record.interaction_mode === 'string' ? record.interaction_mode : undefined,
  }
}

function parsePlanNode(value: unknown): PlanNodeView | null {
  const record = recordOf(value)
  if (!record) return null
  const nodeId = typeof record.node_id === 'string' ? record.node_id.trim() : ''
  const kind = typeof record.kind === 'string' ? record.kind.trim() : ''
  if (!nodeId || !kind) return null
  return {
    node_id: nodeId,
    kind,
    depth: numberOrUndefined(record.depth) ?? 0,
    depends_on: stringList(record.depends_on),
    owned_paths: stringList(record.owned_paths),
    child_run_id: typeof record.child_run_id === 'string' ? record.child_run_id : null,
    worktree_id: typeof record.worktree_id === 'string' ? record.worktree_id : null,
    required: typeof record.required === 'boolean' ? record.required : undefined,
    effect_class: typeof record.effect_class === 'string' ? record.effect_class : undefined,
    acceptance_criteria: stringList(record.acceptance_criteria),
    timeout_seconds: numberOrUndefined(record.timeout_seconds),
    max_retries: numberOrUndefined(record.max_retries),
  }
}

function parsePlanGraph(value: unknown): PlanGraphView | null {
  const record = recordOf(value)
  if (!record) return null
  const nodes = Array.isArray(record.nodes)
    ? record.nodes.map(parsePlanNode).filter((node): node is PlanNodeView => node !== null)
    : []
  return {
    nodes,
    revision: numberOrUndefined(record.revision),
    max_concurrent_children: numberOrUndefined(record.max_concurrent_children),
    max_child_depth: numberOrUndefined(record.max_child_depth),
  }
}

function parseTodo(value: unknown): TodoView | null {
  const record = recordOf(value)
  if (!record) return null
  const todoId = typeof record.todo_id === 'string'
    ? record.todo_id.trim()
    : typeof record.id === 'string' ? record.id.trim() : ''
  if (!todoId) return null
  const title = typeof record.title === 'string' && record.title.trim() ? record.title.trim() : todoId
  const evidence = Array.isArray(record.evidence) ? record.evidence.length : 0
  return {
    todo_id: todoId,
    title,
    status: typeof record.status === 'string' ? record.status : 'pending',
    active_sessions: stringList(record.active_sessions),
    updated_sequence: numberOrUndefined(record.updated_sequence),
    evidence_count: evidence,
  }
}

function runFactsFromProjection(projection: unknown): RunFacts {
  const record = recordOf(projection)
  if (!record) return emptyRunFacts
  const todos = Array.isArray(record.todos)
    ? record.todos.map(parseTodo).filter((todo): todo is TodoView => todo !== null)
    : []
  return {
    goal: parseGoalContract(record.goal),
    plan: parsePlanGraph(record.plan),
    todos,
    discovery_status: typeof record.discovery_status === 'string' ? record.discovery_status : undefined,
    mutation_gate: typeof record.mutation_gate === 'string' ? record.mutation_gate : undefined,
  }
}

function reduceRunFacts(current: RunFacts, event: EventItem): RunFacts {
  const payload = event.payload ?? {}
  if (['RunCreated', 'GoalContractAccepted', 'GoalContractRevised'].includes(event.event_type)) {
    const goal = parseGoalContract(payload.goal)
    return goal ? { ...current, goal } : current
  }
  if (['PlanCreated', 'PlanRevised'].includes(event.event_type)) {
    const plan = parsePlanGraph(payload.plan)
    return plan ? { ...current, plan } : current
  }
  if (['TodoCreated', 'TodoUpdated'].includes(event.event_type)) {
    const todo = parseTodo(payload.todo)
    if (!todo) return current
    const todos = current.todos.some((item) => item.todo_id === todo.todo_id)
      ? current.todos.map((item) => item.todo_id === todo.todo_id ? { ...item, ...todo } : item)
      : [...current.todos, todo]
    return { ...current, todos }
  }
  if (event.event_type === 'TodoCompleted') {
    const todoId = typeof payload.todo_id === 'string' ? payload.todo_id : ''
    if (!todoId) return current
    const todos = current.todos.some((item) => item.todo_id === todoId)
      ? current.todos.map((item) => item.todo_id === todoId ? { ...item, status: 'done', updated_sequence: event.sequence } : item)
      : [...current.todos, { todo_id: todoId, title: todoId, status: 'done', active_sessions: [], updated_sequence: event.sequence, evidence_count: 0 }]
    return { ...current, todos }
  }
  return current
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

function useModalFocusTrap(dialogRef: { current: HTMLElement | null }, onClose: () => void) {
  const closeRef = useRef(onClose)
  useEffect(() => { closeRef.current = onClose }, [onClose])
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return undefined
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const selector = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
    const focusable = () => [...dialog.querySelectorAll<HTMLElement>(selector)].filter((item) => item.offsetParent !== null)
    focusable()[0]?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) {
        event.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      previous?.focus()
    }
  }, [dialogRef])
}

function SettingsModal({ initial, onClose, onSaved }: { initial: SettingsState; onClose: () => void; onSaved: (settings: SettingsState) => void }) {
  const dialogRef = useRef<HTMLElement | null>(null)
  useModalFocusTrap(dialogRef, onClose)
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
      <section ref={dialogRef} className="settings-modal" role="dialog" aria-modal="true" aria-label="设置">
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

function KeyboardShortcutsModal({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLElement | null>(null)
  useModalFocusTrap(dialogRef, onClose)
  const shortcuts = [
    ['Ctrl + N', '新建会话'],
    ['Ctrl + K', '搜索会话'],
    ['Ctrl + ,', '打开设置'],
    ['Ctrl + /', '查看快捷键'],
    ['Enter', '发送消息'],
    ['Shift + Enter', '在输入框内换行'],
    ['Esc', '关闭弹窗或菜单'],
  ]
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialogRef} className="shortcuts-modal" role="dialog" aria-modal="true" aria-label="键盘快捷键" tabIndex={-1}>
      <div className="modal-heading"><div><span className="eyebrow">KEYBOARD SHORTCUTS</span><h2>键盘快捷键</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭快捷键"><X size={19} /></button></div>
      <div className="shortcut-list">{shortcuts.map(([keys, label]) => <div className="shortcut-row" key={keys}><span>{label}</span><kbd>{keys}</kbd></div>)}</div>
      <p className="modal-intro">Windows 使用 Ctrl；macOS 使用 Command。</p>
    </section>
  </div>
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
  const structured = event.error?.message?.trim()
  const raw = String(event.payload?.error_kind ?? event.payload?.reason ?? '').trim()
  let detail = ''
  if (structured) detail = ': ' + structured
  else if (raw && raw !== event.event_type) {
    const safe = runtimeDiagnosisDetail(raw)
    if (safe) detail = ': ' + safe
  }
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

const toolLifecycleTypes = new Set([
  'ActionPlanned', 'ActionPrepared', 'ActionStarted', 'ActionSucceeded',
  'ActionFailed', 'ActionCancelled', 'ActionOutcomeUnknown', 'ToolObservationCommitted',
])

function actionIdOf(event: EventItem) {
  const value = event.payload?.action_id
  return typeof value === 'string' && value ? value : null
}

function publicStringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function toolArgumentSummary(value: Record<string, unknown> | undefined) {
  if (!value) return ''
  const preferred = Object.entries(value).find(([key]) => /path|file|url|command/i.test(key))
  const entry = preferred ?? Object.entries(value)[0]
  if (!entry) return ''
  const [key, raw] = entry
  const text = typeof raw === 'string' ? raw : JSON.stringify(raw)
  return `${key}: ${String(text ?? '').replace(/\s+/g, ' ').slice(0, 92)}`
}

function ToolActivityCard({ events, live, onCopy, onOpenPath, onContinueResult, canContinueResult }: {
  events: EventItem[]
  live: boolean
  onCopy: (text: string) => void
  onOpenPath: (path: string) => void
  onContinueResult: (observationId: string, cursor: string) => void
  canContinueResult: (observation: EventItem) => boolean
}) {
  const [expanded, setExpanded] = useState(live)
  const planned = events.find((event) => event.event_type === 'ActionPlanned')
  const observation = events.find((event) => event.event_type === 'ToolObservationCommitted')
  const canContinue = observation ? canContinueResult(observation) : false
  const terminal = [...events].reverse().find((event) => [
    'ActionSucceeded', 'ActionFailed', 'ActionCancelled', 'ActionOutcomeUnknown',
  ].includes(event.event_type))
  const status = observation?.status === 'rejected'
    ? 'rejected'
    : terminal?.event_type === 'ActionSucceeded' || observation?.status === 'succeeded'
      ? 'succeeded'
      : terminal?.event_type === 'ActionFailed' || observation?.status === 'failed'
        ? 'failed'
        : terminal?.event_type === 'ActionOutcomeUnknown' || observation?.status === 'unknown'
          ? 'unknown'
          : terminal?.event_type === 'ActionCancelled' || observation?.status === 'cancelled'
            ? 'cancelled'
            : events.some((event) => event.event_type === 'ActionStarted') ? 'started' : 'planned'
  const toolName = planned?.tool_name ?? observation?.tool_name ?? '工具调用'
  const args = planned?.tool_arguments
  const argumentSummary = toolArgumentSummary(args)
  const output = observation?.content ?? ''
  const paths = [...new Set([
    ...publicStringArray(observation?.read_paths),
    ...publicStringArray(observation?.modified_paths),
  ])]
  const started = events.find((event) => event.event_type === 'ActionStarted')
  const startedAt = started ? Date.parse(started.occurred_at) : NaN
  const endedAt = terminal ? Date.parse(terminal.occurred_at) : NaN
  const measuredDuration = observation?.duration_ms
    ?? (Number.isFinite(startedAt) && Number.isFinite(endedAt) ? Math.max(0, endedAt - startedAt) : undefined)
  const active = status === 'planned' || status === 'started'
  useEffect(() => {
    if (live && active) setExpanded(true)
    else if (!live && !active) setExpanded(false)
  }, [active, live])
  const lifecycle = events.filter((event) => toolLifecycleTypes.has(event.event_type))

  return (
    <details className={'tool-card tool-activity ' + status} open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>
        <span className="tool-summary">
          <span className="tool-icon">{active ? <LoaderCircle size={13} className="spin" /> : status === 'succeeded' ? <Check size={13} /> : status === 'failed' || status === 'unknown' ? <CircleAlert size={13} /> : <Play size={13} />}</span>
          <span className="tool-name">{toolName}</span>
          {argumentSummary && <span className="tool-argument-summary" title={argumentSummary}>{argumentSummary}</span>}
          <span className={'tool-status ' + (status === 'succeeded' ? 'ok' : status === 'failed' || status === 'unknown' ? 'failed' : status === 'rejected' || status === 'cancelled' ? 'rejected' : '')}>{toolStatusLabel(status)}</span>
        </span>
        <ChevronDown size={15} />
      </summary>
      {expanded && <div className="tool-activity-body">
        <div className="tool-activity-meta">
          {measuredDuration != null && <span>{Math.max(0, measuredDuration) < 1000 ? `${Math.round(measuredDuration)} 毫秒` : `${(measuredDuration / 1000).toFixed(1)} 秒`}</span>}
          {observation?.exit_code != null && <span>退出码 {observation.exit_code}</span>}
          {observation?.complete === false && <span>结果未完整读取</span>}
        </div>
        {args && <section className="tool-activity-section"><div><strong>参数</strong><button className="text-button" onClick={(event) => { event.stopPropagation(); onCopy(JSON.stringify(args, null, 2)) }}><Copy size={12} />复制</button></div><pre>{JSON.stringify(args, null, 2)}</pre></section>}
        {paths.length > 0 && <div className="tool-path-list">{paths.map((path) => <button key={path} className="tool-path-chip" onClick={() => onOpenPath(path)} title="在产物面板中打开">{path}</button>)}</div>}
        {(output || observation?.result_truncated) && <section className="tool-activity-section"><div><strong>结果</strong>{observation?.result_lines_total != null && <span>{observation.result_lines_shown ?? 0} / {observation.result_lines_total} {observation.result_lines_unit ?? '行'}</span>}<button className="text-button" onClick={(event) => { event.stopPropagation(); onCopy(output) }} disabled={!output}><Copy size={12} />复制</button></div>{output && <pre>{output}</pre>}{(observation?.result_truncated || observation?.complete === false) && <div className="tool-result-continuation"><small>{observation?.complete === false ? `完整结果已落盘；续读游标 ${observation.next_cursor ?? '可用'}。` : '当前只显示有界预览，完整输出已保存在 Runtime 工具结果中。'}</small>{observation?.complete === false && observation.observation_id && observation.next_cursor && canContinue && <button className="text-button" onClick={(event) => { event.stopPropagation(); onContinueResult(observation.observation_id!, observation.next_cursor!) }}><RotateCcw size={12} />继续读取完整结果</button>}{observation?.complete === false && !canContinue && <small>源运行已结束；当前 Runtime 不接受跨运行续读。</small>}</div>}</section>}
        {!output && !args && <small className="tool-activity-empty">Runtime 仅返回了工具状态，没有额外结果内容。</small>}
        <div className="tool-activity-trace">{lifecycle.map((event) => <span key={event.id}>{runtimeEventLabels[event.event_type] ?? event.event_type}</span>)}</div>
      </div>}
    </details>
  )
}

const TurnProcess = memo(function TurnProcess({ events, live, onCopy, onOpenPath, onContinueResult, canContinueResult }: { events: EventItem[]; live: boolean; onCopy: (text: string) => void; onOpenPath: (path: string) => void; onContinueResult: (observationId: string, cursor: string) => void; canContinueResult: (observation: EventItem) => boolean }) {
  const [expanded, setExpanded] = useState(live)
  // Open the live process automatically, but do not force a user-collapsed
  // historical process back open on every SSE refresh.
  useEffect(() => {
    if (live) setExpanded(true)
  }, [live])
  if (events.length === 0) return null
  const toolCount = new Set(events.filter((event) => toolLifecycleTypes.has(event.event_type)).map(actionIdOf).filter(Boolean)).size
    + events.filter((event) => event.kind === 'tool' && !actionIdOf(event)).length
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
        {(() => {
          const renderedActions = new Set<string>()
          return events.map((event) => {
            const actionId = actionIdOf(event)
            if (actionId && toolLifecycleTypes.has(event.event_type)) {
              if (renderedActions.has(actionId)) return null
              renderedActions.add(actionId)
              const related = events.filter((candidate) => actionIdOf(candidate) === actionId && toolLifecycleTypes.has(candidate.event_type))
              return <ToolActivityCard key={'action-' + actionId} events={related} live={live && related.some((item) => ['ActionPlanned', 'ActionStarted'].includes(item.event_type))} onCopy={onCopy} onOpenPath={onOpenPath} onContinueResult={onContinueResult} canContinueResult={canContinueResult} />
            }
            return <MessageCard event={event} key={event.id} onCopy={onCopy} />
          })
        })()}
      </div>}
    </details>
  )
})

const MessageCard = memo(function MessageCard({ event, onCopy }: { event: EventItem; onCopy: (text: string) => void }) {
  if (event.kind === 'user') {
    return <article className="message user-message"><div className="message-avatar user-avatar">你</div><div className="message-body"><div className="message-label">你</div><div className="message-content">{event.content}</div></div></article>
  }
  if (event.kind === 'assistant') {
    const streaming = Boolean(event.optimistic)
    const content = streaming && event.stream_state
      ? <StreamingMessage state={event.stream_state} text={event.content ?? ''} />
      : streaming
        ? <div className="message-content">{event.content}</div>
      : <div className="markdown message-content" dangerouslySetInnerHTML={renderMarkdown(event.content ?? '')} />
    return <article className={'message assistant-message ' + (streaming ? 'streaming-message' : '')}><div className="message-avatar assistant-avatar"><Sparkles size={15} /></div><div className="message-body"><div className="message-header"><div className="message-label">cc-harness</div>{event.content && !streaming && <button className="message-action" onClick={() => onCopy(event.content ?? '')} title="复制回复" aria-label="复制回复"><Copy size={14} /></button>}</div>{content}{streaming && !event.stream_state && <span className="streaming-cursor" aria-label="正在生成" />}</div></article>
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

function ApprovalCard({ approval, onApprove, onReject, busy = false, toolArguments }: { approval: PendingApproval; onApprove: () => void; onReject: () => void; busy?: boolean; toolArguments?: Record<string, unknown> }) {
  const action = approval.actionId || '需要授权的动作'
  return (
    <div className="approval-card" role="region" aria-label="待处理审批">
      <div className="approval-heading"><ShieldCheck size={17} /><span><strong>需要你的批准</strong><small>Runtime 正在等待这项本机操作；仍可输入消息，审批后按顺序发送</small></span></div>
      <div className="approval-detail"><span>动作</span><b>{action}</b>{approval.scope.length > 0 && <><span>范围</span><b className="approval-scope" title={approval.scope.join('\n')}>{approval.scope.join('、')}</b></>}</div>
      {toolArguments && <details className="approval-arguments"><summary>查看已脱敏的参数</summary><pre>{JSON.stringify(toolArguments, null, 2)}</pre></details>}
      <div className="approval-actions"><button className="secondary-button" onClick={onReject} disabled={busy}>{busy ? '处理中…' : '拒绝'}</button><button className="primary-button" onClick={onApprove} disabled={busy}>{busy ? '处理中…' : '允许一次'}</button></div>
    </div>
  )
}

function streamingHint(state?: StreamingState | null): string {
  if (state?.error) return '本轮输出已冻结，正在从 Runtime 对账'
  switch (state?.phase) {
    case 'reasoning': return '思考中…'
    case 'tool': return `正在调用 ${state.tool_name || '工具'}…`
    case 'content': return '正在生成回复'
    case 'done': return '正在整理回复'
    case 'gap': return '实时流暂时中断，正在从 Runtime 对账'
    default: return 'Runtime 正在工作'
  }
}

function displayApiError(reason: unknown): string {
  const error = reason as ApiError
  let fallback = '请求失败'
  if (typeof reason === 'string' && reason.trim()) fallback = reason
  else if (reason instanceof Error && typeof reason.message === 'string' && reason.message.trim()) fallback = reason.message
  const message = typeof error?.message === 'string' && error.message.trim()
    ? error.message
    : fallback
  const details: string[] = []
  if (typeof error?.next_action === 'string' && error.next_action.trim()) details.push(error.next_action)
  if (error?.retryable) details.push('可重试')
  if (error?.partial_output) details.push('已保留部分输出，未自动重放')
  if (typeof error?.retry_after === 'number' && Number.isFinite(error.retry_after) && error.retry_after > 0) {
    details.push(`${Math.ceil(error.retry_after)} 秒后可重试`)
  }
  if (typeof error?.attempt === 'number' && Number.isFinite(error.attempt) && error.attempt > 0) {
    details.push(`第 ${error.attempt} 次尝试`)
  }
  const phaseLabels: Record<string, string> = {
    connection: '连接阶段', model: '模型阶段', tool: '工具阶段',
    approval: '审批阶段', persistence: '状态存储阶段', sse: '实时连接阶段',
  }
  if (typeof error?.phase === 'string' && Object.prototype.hasOwnProperty.call(phaseLabels, error.phase)) {
    details.push(phaseLabels[error.phase])
  }
  if (typeof error?.request_id === 'string' && error.request_id.trim()) details.push(`请求编号 ${error.request_id}`)
  return [message, ...details].join(' · ')
}

const todoStatusLabels: Record<string, string> = {
  pending: '待处理',
  ready: '待处理',
  in_progress: '执行中',
  running: '执行中',
  done: '已完成',
  completed: '已完成',
  blocked: '已阻塞',
  failed: '失败',
  cancelled: '已停止',
}

function todoStatusLabel(value: string | undefined) {
  return todoStatusLabels[value ?? ''] ?? value ?? '待处理'
}

function interactionModeLabel(value: string | undefined) {
  return value === 'conversation' ? '对话' : value === 'coding' ? '编码' : value || '未声明'
}

function ContractField({ label, values }: { label: string; values: string[] }) {
  return (
    <div className="runtime-contract-field">
      <span>{label}</span>
      {values.length === 0
        ? <div className="runtime-contract-field-empty">未声明</div>
        : <ul className="runtime-contract-field-list">{values.map((value, index) => <li key={label + index}>{value}</li>)}</ul>}
    </div>
  )
}

function RuntimeContractPopover({ facts, status, sequence, onClose }: { facts: RunFacts; status: string; sequence?: number; onClose: () => void }) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [planExpanded, setPlanExpanded] = useState(false)
  const [collapsedNodes, setCollapsedNodes] = useState<Record<string, boolean>>({})
  const goal = facts.goal
  const plan = facts.plan
  const todoById = useMemo(() => new Map(facts.todos.map((todo) => [todo.todo_id, todo])), [facts.todos])
  const planNodes = plan?.nodes ?? []
  const parentByNode = useMemo(() => {
    const stack: Array<{ depth: number; nodeId: string }> = []
    const parents: Record<string, string | undefined> = {}
    for (const node of planNodes) {
      while (stack.length > 0 && stack[stack.length - 1].depth >= node.depth) stack.pop()
      parents[node.node_id] = stack[stack.length - 1]?.nodeId
      stack.push({ depth: node.depth, nodeId: node.node_id })
    }
    return parents
  }, [planNodes])
  const hasChildren = useMemo(() => new Set(Object.values(parentByNode).filter((value): value is string => Boolean(value))), [parentByNode])
  const isHiddenByCollapsedParent = (nodeId: string) => {
    let parent = parentByNode[nodeId]
    while (parent) {
      if (collapsedNodes[parent]) return true
      parent = parentByNode[parent]
    }
    return false
  }
  const visibleNodes = (planExpanded ? planNodes : planNodes.slice(0, 6)).filter((node) => !isHiddenByCollapsedParent(node.node_id))
  const completedTodos = planNodes.length > 0
    ? planNodes.filter((node) => ['done', 'completed'].includes(todoById.get(node.node_id)?.status ?? '')).length
    : facts.todos.filter((todo) => ['done', 'completed'].includes(todo.status)).length
  const totalPlanItems = planNodes.length || facts.todos.length
  const planStatus = planNodes.length > 0
    ? `${completedTodos}/${totalPlanItems} 个 Todo 已完成`
    : facts.todos.length > 0 ? `${completedTodos}/${facts.todos.length} 个 Todo 已完成` : '等待 Runtime 写入计划'
  const knownNodeIds = new Set(planNodes.map((node) => node.node_id))
  const extraTodos = planNodes.length > 0 ? facts.todos.filter((todo) => !knownNodeIds.has(todo.todo_id)) : []
  return (
    <section className="runtime-contract-popover" role="dialog" aria-label="目标契约与执行计划">
      <div className="runtime-contract-popover-header">
        <div>
          <strong>目标契约与执行计划</strong>
          <small>{statusLabels[status] ?? status}{sequence != null ? ` · 事件 #${sequence}` : ''}</small>
        </div>
        <button className="icon-button compact" type="button" onClick={onClose} aria-label="关闭目标契约" title="关闭"><X size={15} /></button>
      </div>

      <section className="runtime-contract-section">
        <div className="runtime-contract-section-heading">
          <span>目标契约</span>
          <span className="runtime-contract-badge">{goal?.contract_version ? `v${goal.contract_version}` : '未知版本'}</span>
        </div>
        {goal
          ? <>
            <p className="runtime-contract-objective">{goal.objective}</p>
            <ul className="runtime-contract-list">{goal.acceptance_criteria.map((item, index) => <li key={'acceptance-' + index}>{item}</li>)}</ul>
            <button className="runtime-contract-toggle" type="button" aria-expanded={detailsOpen} onClick={() => setDetailsOpen((value) => !value)}>
              <ChevronDown size={13} />{detailsOpen ? '收起约束与边界' : '展开约束与边界'}
            </button>
            {detailsOpen && <div className="runtime-contract-details">
              <ContractField label="约束" values={goal.constraints} />
              <ContractField label="允许范围" values={goal.allowed_scope} />
              <ContractField label="排除范围" values={goal.excluded_scope} />
              <ContractField label="所需证据" values={goal.required_evidence} />
              <ContractField label="人工复核" values={goal.human_review} />
              <div className="runtime-contract-field"><span>交互模式</span><div>{interactionModeLabel(goal.interaction_mode)}</div></div>
            </div>}
          </>
          : <p className="runtime-contract-empty">Runtime 尚未在当前投影中写入目标契约。</p>}
      </section>

      <section className="runtime-contract-section">
        <div className="runtime-contract-section-heading">
          <span>PlanGraph / Todo</span>
          {plan?.revision != null && <span className="runtime-contract-badge">修订 {plan.revision}</span>}
        </div>
        <div className="runtime-plan-summary">{planStatus}{plan?.max_concurrent_children ? ` · 并行上限 ${plan.max_concurrent_children}` : ''}</div>
        {visibleNodes.length > 0
          ? <div className="runtime-plan-list">
            {visibleNodes.map((node) => {
              const todo = todoById.get(node.node_id)
              const nodeStatus = todo?.status ?? 'pending'
              const classStatus = nodeStatus.replace(/[^a-z0-9_-]/gi, '-')
              const title = todo?.title || node.kind + ' · ' + node.node_id
              const metadata = [
                todoStatusLabel(nodeStatus),
                node.kind === 'child' ? '子任务' : '主任务',
                node.required === false ? '可选' : '',
                node.depends_on.length > 0 ? `${node.depends_on.length} 个依赖` : '',
                node.acceptance_criteria.length > 0 ? `${node.acceptance_criteria.length} 条验收` : '',
                node.owned_paths.length > 0 ? `${node.owned_paths.length} 个路径` : '',
                todo?.evidence_count ? `${todo.evidence_count} 条证据` : '',
              ].filter(Boolean).join(' · ')
              return <div className={'runtime-plan-node todo-' + classStatus} key={node.node_id} style={{ paddingLeft: Math.min(4, Math.max(0, node.depth)) * 14 + 6 }}>
                {hasChildren.has(node.node_id) && <button className="runtime-plan-node-toggle" type="button" onClick={() => setCollapsedNodes((current) => ({ ...current, [node.node_id]: !current[node.node_id] }))} aria-expanded={!collapsedNodes[node.node_id]} aria-label={collapsedNodes[node.node_id] ? '展开子任务' : '收起子任务'} title={collapsedNodes[node.node_id] ? '展开子任务' : '收起子任务'}><ChevronDown size={12} /></button>}
                <span className="runtime-plan-node-mark" />
                <div className="runtime-plan-node-copy">
                  <strong className="runtime-plan-node-title" title={title}>{title}</strong>
                  <span className="runtime-plan-node-meta">{metadata}</span>
                </div>
                {node.timeout_seconds != null && <span className="runtime-plan-node-extra" title="步骤超时上限">{node.timeout_seconds}s</span>}
              </div>
            })}
          </div>
          : facts.todos.length === 0
            ? <p className="runtime-contract-empty">暂无 Runtime 执行计划数据。</p>
            : <div className="runtime-plan-list">{facts.todos.map((todo) => <div className={'runtime-plan-node todo-' + todo.status.replace(/[^a-z0-9_-]/gi, '-')} key={todo.todo_id}><span className="runtime-plan-node-mark" /><div className="runtime-plan-node-copy"><strong className="runtime-plan-node-title" title={todo.title}>{todo.title}</strong><span className="runtime-plan-node-meta">{todoStatusLabel(todo.status)}{todo.evidence_count ? ` · ${todo.evidence_count} 条证据` : ''}</span></div></div>)}</div>}
        {extraTodos.length > 0 && <div className="runtime-plan-extra">另有 {extraTodos.length} 个 Todo 未映射到当前计划节点</div>}
        {planNodes.length > 6 && <button className="runtime-plan-expand" type="button" onClick={() => setPlanExpanded((value) => !value)}>{planExpanded ? '收起计划' : `展开全部 ${planNodes.length} 个节点`}</button>}
      </section>

      <div className="runtime-contract-source">只读 · 来源：Durable Runtime 投影 / 不可变事件流{facts.discovery_status ? ` · 拆解：${facts.discovery_status}` : ''}{facts.mutation_gate ? ` · 变更闸门：${facts.mutation_gate}` : ''}</div>
    </section>
  )
}

function RuntimeProgress({ status, sequence, streaming, events, hasApproval, facts }: { status: string; sequence?: number; streaming: StreamingState | null; events: EventItem[]; hasApproval: boolean; facts: RunFacts }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const stage = currentRuntimeStage(status, streaming, events, hasApproval)
  const activeIndex = stage === 'done' ? runtimeStages.length - 1 : Math.max(0, runtimeStages.indexOf(stage))
  const progress = stage === 'done' ? 360 : ((activeIndex + 1) / runtimeStages.length) * 360
  const stageCopy = status === 'queued'
      ? '已接收，等待 Runtime 调度'
      : status === 'waiting_on_predecessor'
        ? '等待前序任务安全结束'
      : status === 'cancel_requested'
        ? '正在保存停止边界'
        : status === 'cancelled'
          ? '已保存停止边界，可继续发送新指令'
        : status === 'stalled' || status === 'blocked' || status === 'failed_recoverable'
          ? '可从当前检查点继续'
          : runtimeStageLabels[stage]
  const hasFacts = Boolean(facts.goal || facts.plan?.nodes.length || facts.todos.length)
  const tone = statusTone(status)

  useEffect(() => {
    if (!open) return undefined
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className={'runtime-contract-anchor stage-' + stage + ' tone-' + tone}>
      <div className="runtime-contract-trigger-row">
        <button className="runtime-status-circle-button" type="button" onClick={() => setOpen((value) => !value)} aria-label="查看目标契约与执行计划" aria-haspopup="dialog" aria-expanded={open} title="查看目标契约与执行计划">
          <span className="runtime-status-circle" style={{ '--runtime-progress': progress + 'deg' } as React.CSSProperties}>
            <span>{stage === 'done' ? <Check size={15} /> : activeIndex + 1}</span>
          </span>
        </button>
        <div className="runtime-contract-trigger-copy">
          <span className="runtime-contract-status"><StatusDot status={status} /><strong>{statusLabels[status] ?? status}</strong><span className="runtime-contract-stage">· {runtimeStageLabels[stage]}</span></span>
          <span className="runtime-contract-hint">{hasFacts ? `点击查看目标契约、PlanGraph 与 Todo · ${stageCopy}` : stageCopy}</span>
        </div>
        {sequence != null && <span className="runtime-contract-sequence">事件 #{sequence}</span>}
      </div>
      {open && <RuntimeContractPopover facts={facts} status={status} sequence={sequence} onClose={() => setOpen(false)} />}
    </div>
  )
}

function QueuePanel({ items }: { items: QueuedMessage[] }) {
  if (items.length === 0) return null
  return (
    <section className="queue-panel" aria-live="polite" aria-label="排队中的消息">
      <div className="queue-panel-heading">
        <span><History size={14} />排队消息</span>
        <strong>{items.length}</strong>
        <small>按顺序处理</small>
      </div>
      <div className="queue-panel-list">
        {items.slice(0, 3).map((item) => (
          <div className="queue-panel-item" key={item.follow_up_run_id}>
            <span className="queue-panel-dot" />
            <span className="queue-panel-content">{item.content}</span>
            <span className="queue-panel-gate">{item.gate === 'approval' ? '审批后发送' : item.gate === 'incomplete' ? '前序未完成' : item.gate === 'bypassed' ? '已转向' : '等待中'}</span>
          </div>
        ))}
      </div>
      {items.length > 3 && <div className="queue-panel-more">另有 {items.length - 3} 条消息等待处理</div>}
    </section>
  )
}

type CapabilityStatus = NonNullable<ContextState['capabilities']>['memory']

type ProjectArtifact = {
  path: string
  change_kind: string
  latest_changed_at: string
  latest_task_title: string
  latest_task_id: string
  latest_run_id: string
  task_count: number
  tasks: Array<{ run_id: string; title: string }>
  exists: boolean
  previewable: boolean
  size_bytes: number | null
  modified_at: string | null
  unsafe?: boolean
}

type ArtifactPreview = {
  path: string
  previewable: boolean
  size_bytes: number | null
  modified_at: string | null
  content: string | null
  reason: string | null
}

function artifactSize(value: number | null) {
  if (value == null) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function artifactIcon(path: string) {
  const extension = path.split('.').pop()?.toLowerCase() ?? ''
  if (['ts', 'tsx', 'js', 'jsx', 'py', 'rs', 'go', 'java', 'c', 'h', 'cpp', 'css', 'html', 'json', 'yml', 'yaml', 'toml', 'md', 'sql'].includes(extension)) return FileCode2
  if (['txt', 'log', 'csv', 'xml'].includes(extension)) return FileText
  return File
}

function ProjectArtifacts({
  projectRoot,
  onChooseProject,
  onCopy,
  onOpen,
  compact = false,
  reloadKey = 0,
  initialPath = null,
}: {
  projectRoot: string | null
  onChooseProject: () => void
  onCopy: (value: string) => void
  onOpen: (path: string, projectRoot: string) => Promise<void>
  compact?: boolean
  reloadKey?: number
  initialPath?: string | null
}) {
  const [files, setFiles] = useState<ProjectArtifact[]>([])
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [openPaths, setOpenPaths] = useState<string[]>([])
  const [preview, setPreview] = useState<ArtifactPreview | null>(null)
  const [query, setQuery] = useState('')
  const [viewMode, setViewMode] = useState<'preview' | 'code'>('code')
  const [loading, setLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [openBusyPath, setOpenBusyPath] = useState<string | null>(null)
  const listGeneration = useRef(0)
  const previewGeneration = useRef(0)
  const selectedPathRef = useRef<string | null>(null)

  const reload = useCallback(async () => {
    const generation = ++listGeneration.current
    if (!projectRoot) {
      setFiles([])
      setSelectedPath(null)
      selectedPathRef.current = null
      setOpenPaths([])
      setPreview(null)
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const result = await webApi<{ project_root: string; files: ProjectArtifact[] }>('/api/artifacts')
      if (generation !== listGeneration.current) return
      if (result.project_root !== projectRoot) {
        setFiles([])
        setSelectedPath(null)
        selectedPathRef.current = null
        setOpenPaths([])
        setError('当前选中的项目已变化，请重新打开产物列表。')
        return
      }
      setFiles(result.files)
      const next = result.files.some((item) => item.path === initialPath)
        ? initialPath
        : result.files.some((item) => item.path === selectedPathRef.current)
          ? selectedPathRef.current
        : result.files[0]?.path ?? null
      selectedPathRef.current = next
      setSelectedPath(next)
      setOpenPaths((paths) => {
        const available = paths.filter((path) => result.files.some((item) => item.path === path))
        return next && !available.includes(next) ? [...available, next].slice(-8) : available
      })
    } catch (reason) {
      if (generation === listGeneration.current) setError(displayApiError(reason))
    } finally {
      if (generation === listGeneration.current) setLoading(false)
    }
  }, [initialPath, projectRoot])

  useEffect(() => { void reload() }, [reload, reloadKey])

  useEffect(() => {
    const generation = ++previewGeneration.current
    if (!projectRoot || !selectedPath) {
      setPreview(null)
      setPreviewLoading(false)
      return
    }
    setPreview(null)
    setPreviewLoading(true)
    webApi<ArtifactPreview>(`/api/artifacts/preview?path=${encodeURIComponent(selectedPath)}&project_root=${encodeURIComponent(projectRoot)}`)
      .then((value) => {
        if (generation === previewGeneration.current) setPreview(value)
      })
      .catch((reason) => {
        if (generation === previewGeneration.current) setError(displayApiError(reason))
      })
      .finally(() => {
        if (generation === previewGeneration.current) setPreviewLoading(false)
      })
  }, [projectRoot, selectedPath])

  const visibleFiles = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return files.filter((item) => !needle || item.path.toLocaleLowerCase().includes(needle))
  }, [files, query])
  const selected = files.find((item) => item.path === selectedPath) ?? null
  const Icon = selected ? artifactIcon(selected.path) : File
  const supportsDocumentPreview = Boolean(selectedPath && /\.(md|html?)$/i.test(selectedPath))

  useEffect(() => {
    setViewMode(supportsDocumentPreview ? 'preview' : 'code')
  }, [selectedPath, supportsDocumentPreview])

  function selectArtifact(path: string) {
    selectedPathRef.current = path
    setSelectedPath(path)
    setOpenPaths((current) => current.includes(path) ? current : [...current, path].slice(-8))
  }

  function closeArtifactTab(path: string) {
    const remaining = openPaths.filter((item) => item !== path)
    setOpenPaths(remaining)
    if (selectedPath === path) {
      const next = remaining[remaining.length - 1] ?? null
      selectedPathRef.current = next
      setSelectedPath(next)
    }
  }

  async function openArtifactFile(path: string) {
    if (!projectRoot) return
    setOpenBusyPath(path)
    setError(null)
    try {
      await onOpen(path, projectRoot)
    } catch (reason) {
      setError(displayApiError(reason))
    } finally {
      setOpenBusyPath(null)
    }
  }

  return (
    <section className={'artifact-page' + (compact ? ' compact-artifact-page' : '') + (fullscreen ? ' artifact-fullscreen' : '')}>
      <header className="artifact-page-header">
        <div className="artifact-title-group">
          <span className="eyebrow">PROJECT FILES</span>
          <h1>产物</h1>
          <p title={projectRoot ?? undefined}>{projectRoot ?? '选择一个项目，查看 Agent 创建或修改过的文件'}</p>
        </div>
        <div className="artifact-header-actions">
          {projectRoot && <span className="artifact-count">{files.length} 个文件</span>}
          <button className="icon-button artifact-fullscreen-button" onClick={() => setFullscreen((value) => !value)} title={fullscreen ? '退出全屏' : '全屏'} aria-label={fullscreen ? '退出产物全屏' : '产物全屏'}>{fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
          <button className="icon-button" onClick={() => void reload()} disabled={loading || !projectRoot} title="刷新产物列表" aria-label="刷新产物列表"><RefreshCw size={16} className={loading ? 'spin' : ''} /></button>
        </div>
      </header>
      {!projectRoot ? (
        <div className="artifact-empty-state">
          <span className="artifact-empty-icon"><FolderOpen size={23} /></span>
          <h2>先选择一个项目</h2>
          <p>产物只会显示当前项目历史任务中由 Agent 创建或修改的文件。</p>
          <button className="primary-button" onClick={onChooseProject}><FolderOpen size={16} />选择项目</button>
        </div>
      ) : (
        <div className="artifact-workspace">
          <aside className="artifact-file-pane">
            <label className="artifact-search"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选项目产物" aria-label="筛选项目产物" /></label>
            <div className="artifact-file-list">
              {loading && files.length === 0 && <div className="artifact-list-message"><LoaderCircle size={17} className="spin" />正在汇总项目历史…</div>}
              {!loading && visibleFiles.length === 0 && <div className="artifact-list-message">{query ? '没有匹配的文件' : '此项目还没有 Agent 修改过的文件'}</div>}
              {visibleFiles.map((item) => {
                const ItemIcon = artifactIcon(item.path)
                return (
                  <button key={item.path} className={'artifact-file-row' + (selectedPath === item.path ? ' selected' : '')} onClick={() => selectArtifact(item.path)}>
                    <ItemIcon size={16} />
                    <span className="artifact-file-copy"><strong title={item.path}>{item.path.split('/').pop()}</strong><small title={item.path}>{item.path.includes('/') ? item.path.slice(0, item.path.lastIndexOf('/')) : '项目根目录'}</small></span>
                    {item.unsafe ? <span className="artifact-file-missing">路径受限</span> : !item.exists && <span className="artifact-file-missing">已删除</span>}
                  </button>
                )
              })}
            </div>
            <div className="artifact-list-footnote">汇总此项目的全部历史任务 · 路径去重</div>
          </aside>
          <section className="artifact-preview-pane" aria-label="产物预览">
            {openPaths.length > 0 && <nav className="artifact-tabs" role="tablist" aria-label="已打开的产物">
              {openPaths.map((path) => <div className={'artifact-tab' + (selectedPath === path ? ' selected' : '')} key={path}>
                <button role="tab" aria-selected={selectedPath === path} title={path} onClick={() => selectArtifact(path)}>{path.split('/').pop()}</button>
                <button className="artifact-tab-close" aria-label={'关闭 ' + path} onClick={() => closeArtifactTab(path)}><X size={12} /></button>
              </div>)}
            </nav>}
            {selected ? (
              <>
                <header className="artifact-preview-header">
                  <div className="artifact-preview-heading"><Icon size={18} /><div><strong title={selected.path}>{selected.path}</strong><span>{selected.exists ? `${artifactSize(selected.size_bytes)} · 当前文件` : '当前文件不存在'}</span></div></div>
                  <div className="artifact-preview-actions">
                    {selected.exists && projectRoot && <button className="text-button artifact-open-file" onClick={() => void openArtifactFile(selected.path)} disabled={openBusyPath === selected.path} title="使用本机默认程序打开"><ExternalLink size={14} />{openBusyPath === selected.path ? '正在打开…' : '在文件中打开'}</button>}
                    {supportsDocumentPreview && <div className="artifact-view-switch" role="tablist" aria-label="产物视图">
                      <button role="tab" aria-selected={viewMode === 'preview'} className={viewMode === 'preview' ? 'selected' : ''} onClick={() => setViewMode('preview')}>预览</button>
                      <button role="tab" aria-selected={viewMode === 'code'} className={viewMode === 'code' ? 'selected' : ''} onClick={() => setViewMode('code')}>代码</button>
                    </div>}
                    {preview?.content != null && <button className="text-button artifact-copy" onClick={() => onCopy(preview.content ?? '')}><Copy size={14} />复制</button>}
                  </div>
                </header>
                <div className="artifact-provenance">
                  <span>最近由任务修改</span><strong title={selected.latest_task_id}>{selected.latest_task_title}</strong>
                  {selected.task_count > 1 && <small>共关联 {selected.task_count} 个历史任务</small>}
                </div>
                <div className="artifact-code-view">
                  {previewLoading && <div className="artifact-preview-message"><LoaderCircle size={18} className="spin" />读取当前文件…</div>}
                  {!previewLoading && preview && !preview.previewable && <div className="artifact-preview-message"><FileText size={20} /><strong>无法预览此文件</strong><span>{preview.reason ?? '当前路径不可安全预览'}</span></div>}
                  {!previewLoading && preview?.previewable && viewMode === 'preview' && supportsDocumentPreview
                    ? <article className="artifact-rendered-view" dangerouslySetInnerHTML={renderMarkdown(preview.content ?? '')} />
                    : !previewLoading && preview?.previewable && <SyntaxCode content={preview.content ?? ''} />}
                  {!previewLoading && !preview && !selected.exists && <div className="artifact-preview-message"><FileText size={20} /><strong>当前文件不存在</strong><span>预览只读取项目当前内容，不保留旧版本。</span></div>}
                  {!previewLoading && !preview && selected.exists && <div className="artifact-preview-message"><FileText size={20} /><strong>文件预览暂不可用</strong><span>检查项目选择后重试，产物预览不会读取其他项目或历史快照。</span></div>}
                </div>
              </>
            ) : (
              <div className="artifact-preview-empty"><FileCode2 size={25} /><strong>选择一个文件</strong><span>这里只显示 Agent 在此项目的历史任务中创建或修改过的文件。</span></div>
            )}
          </section>
        </div>
      )}
      {error && <div className="artifact-error" role="status"><AlertCircle size={15} /><span>{error}</span><button onClick={() => setError(null)} aria-label="关闭错误"><X size={14} /></button></div>}
    </section>
  )
}

function capabilityStatusLabel(value?: CapabilityStatus) {
  if (!value) return '暂无数据'
  if (value.enabled === false || value.details?.configured_enabled === false) return '已关闭'
  if (!value.initialized) return '未初始化'
  if (value.degraded_reason) return '已降级'
  const degraded = value.details?.degraded_reasons
  if (Array.isArray(degraded) && degraded.length > 0) return '部分降级'
  return value.triggered ? '已启用' : '待触发'
}

function RuntimeContextCard({ context }: { context: ContextState }) {
  const percent = context.ratio == null ? null : Math.min(100, Math.max(0, context.ratio * 100))
  const categories = Object.entries(context.categories ?? {}).filter(([, value]) => value > 0).sort((left, right) => right[1] - left[1]).slice(0, 3)
  const categoryLabels: Record<string, string> = { user_input: '对话消息', system_prompt: '系统提示词', tool_calls: '工具调用', tool_definitions: '工具定义', llm_output: '模型输出', summary: '压缩摘要' }
  const compaction = context.compaction
  const memoryDetails = context.capabilities?.memory?.details ?? {}
  const retrievalPath = Array.isArray(memoryDetails.retrieval_path) ? memoryDetails.retrieval_path.join(' → ') : typeof memoryDetails.retrieval_path === 'string' ? memoryDetails.retrieval_path : null
  return (
    <section className="inspector-card context-inspector-card" aria-label="上下文与能力">
      <div className="card-heading"><div><span className="card-eyebrow">上下文与能力</span><small>Runtime 最近一次请求</small></div>{percent != null && <span className="context-inspector-percent">{percent.toFixed(1)}%</span>}</div>
      <div className="context-inspector-total"><strong>{formatTokens(context.used_tokens)}</strong><span>/ {formatTokens(context.window_tokens)}</span></div>
      <div className="context-inspector-track"><span style={{ width: percent == null ? '0%' : percent + '%' }} /></div>
      {categories.length > 0 && <div className="context-inspector-categories">{categories.map(([key, value]) => <div key={key}><span>{categoryLabels[key] ?? key}</span><b>{formatTokens(value)}</b></div>)}</div>}
      <div className="context-inspector-facts">
        <div><span>压缩</span><b>{compaction?.applied ? `已${compaction.tier === 'summarize' ? '总结' : compaction.tier === 'prune' ? '裁剪' : '缩减'}` : '未触发'}</b></div>
        <div><span>记忆</span><b>{capabilityStatusLabel(context.capabilities?.memory)}</b></div>
        <div><span>安全</span><b>{capabilityStatusLabel(context.capabilities?.safety)}</b></div>
      </div>
      {retrievalPath && <div className="context-inspector-retrieval"><span>记忆检索</span><strong>{retrievalPath}</strong></div>}
      {!context.used_tokens && !context.capabilities?.context && <div className="context-inspector-empty"><Info size={14} />暂无 Runtime 上下文数据</div>}
    </section>
  )
}

function App() {
  const [mainPage, setMainPage] = useState<'chat' | 'artifacts'>('chat')
  const [rightPane, setRightPane] = useState<'runtime' | 'artifacts'>('runtime')
  const [artifactRefreshKey, setArtifactRefreshKey] = useState(0)
  const [artifactTargetPath, setArtifactTargetPath] = useState<string | null>(null)
  const [project, setProject] = useState<{ root: string } | null>(null)
  const [settings, setSettings] = useState<SettingsState>({ base_url: '', model: '', permission_mode: 'default', has_api_key: false, api_key_masked: '' })
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [optimisticEvents, setOptimisticEvents] = useState<Record<string, EventItem[]>>({})
  const [streamingRuns, setStreamingRuns] = useState<Record<string, StreamingState>>({})
  const [context, setContext] = useState<ContextState>(emptyContext)
  const [runFacts, setRunFacts] = useState<RunFacts>(emptyRunFacts)
  const [executor, setExecutor] = useState<ExecutorState>(emptyExecutor)
  const [scheduler, setScheduler] = useState<SchedulerState>(emptyScheduler)
  const [webCapabilities, setWebCapabilities] = useState<CapabilityManifest | null>(null)
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([])
  const [optimisticQueue, setOptimisticQueue] = useState<Record<string, QueuedMessage[]>>({})
  const [approvalBusyId, setApprovalBusyId] = useState<string | null>(null)
  const [continuationNotice, setContinuationNotice] = useState<string | null>(null)
  const [sseStatus, setSseStatus] = useState<'connected' | 'reconnecting' | 'stale'>('connected')
  const [sseNotice, setSseNotice] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [legacyEvents, setLegacyEvents] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [deletingSession, setDeletingSession] = useState<string | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const [contextOpen, setContextOpen] = useState(false)
  const [contextAnchor, setContextAnchor] = useState<'bottom' | null>(null)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(true)
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
  const sseHealthReasonRef = useRef<string | null>(null)
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
  // Segment commits block late deltas for that segment; terminal run events
  // block any later chunks until the user explicitly starts a follow-up.
  const finalizedStreamRunsRef = useRef<Set<string>>(new Set())
  const finalizedStreamSegmentsRef = useRef<Set<string>>(new Set())
  const continuationGeneration = useRef(0)
  const conversationRef = useRef<HTMLElement | null>(null)
  const streamOpened = useRef(false)
  const sseRetryCount = useRef(0)
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
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    const height = Math.min(textarea.scrollHeight, 240)
    textarea.style.height = `${height}px`
    textarea.style.overflowY = textarea.scrollHeight > 240 ? 'auto' : 'hidden'
  }, [draft])

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
      const manifest = await webApi<CapabilityManifest>('/api/web/v1/capabilities')
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
      setError(displayApiError(reason))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    const closeInspectorOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !rightCollapsed && !settingsOpen) setRightCollapsed(true)
    }
    document.addEventListener('keydown', closeInspectorOnEscape)
    return () => document.removeEventListener('keydown', closeInspectorOnEscape)
  }, [rightCollapsed, settingsOpen])

  const loadSession = useCallback(async (runId: string) => {
    if (hiddenSessionsRef.current.has(runId)) return
    const requestGeneration = ++loadGeneration.current
    try {
      // The timeline and run snapshot are needed to paint the selected
      // conversation. The all-project sidebar scan can be much slower (it
      // replays every project root), so fetch it after the conversation is
      // already visible instead of making a session switch wait for it.
      const [timeline, state] = await Promise.all([
        webApi<{ events: EventItem[] }>('/api/sessions/' + encodeURIComponent(runId) + '/timeline?limit=10000'),
        webApi<{ project_root?: string; context: ContextState; status: string; executor?: ExecutorState; scheduler?: SchedulerState; projection?: { goal?: unknown; plan?: unknown; todos?: unknown; discovery_status?: unknown; mutation_gate?: unknown; queue?: QueueProjection[]; approvals?: Array<{ approval_id: string; run_id?: string; action_args_digest: string; action_id?: string; scope?: string[]; status: string }> } }>('/api/sessions/' + encodeURIComponent(runId)),
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
      setRunFacts(runFactsFromProjection(state.projection))
      setExecutor(state.executor ?? emptyExecutor)
      setScheduler(state.scheduler ?? emptyScheduler)
      // ApprovalRequested remains immutable evidence even after a Run is
      // cancelled.  Only an awaiting_approval Run has an actionable card;
      // terminal snapshots must clear any card left from a prior render.
      setPendingApprovals(state.status === 'awaiting_approval'
        ? (state.projection?.approvals ?? []).filter((item) => item.status === 'requested').map((item) => ({ approvalId: item.approval_id, digest: item.action_args_digest, actionId: item.action_id, scope: item.scope ?? [], runId: item.run_id }))
        : [])
      // Optimistic queue rows are only a transport acknowledgement. Remove
      // them as soon as the durable FollowUpQueued event is visible; if the
      // timeline is truncated, keep the optimistic row until a later refresh
      // rather than claiming that Runtime lost the message.
      const durableQueueIds = new Set(timeline.events
        .filter((event) => event.event_type === 'FollowUpQueued')
        .map((event) => String(event.payload?.follow_up_run_id ?? ''))
        .filter(Boolean))
      const durableQueueContent = new Set(timeline.events
        .filter((event) => event.event_type === 'FollowUpQueued')
        .map((event) => normalizedUserContent(event.content))
        .filter(Boolean))
      setOptimisticQueue((current) => {
        const pending = current[runId] ?? []
        if (pending.length === 0 || (durableQueueIds.size === 0 && durableQueueContent.size === 0)) return current
        const remaining = pending.filter((item) => {
          if (durableQueueIds.has(item.follow_up_run_id)) return false
          // Only the defensive client-generated id has no durable id to match.
          // Content matching is restricted to that case so two identical user
          // messages queued in one session are never collapsed together.
          return !(item.optimistic && item.follow_up_run_id.startsWith('pending-') && durableQueueContent.has(item.content))
        })
        if (remaining.length === pending.length) return current
        const next = { ...current }
        if (remaining.length === 0) delete next[runId]
        else next[runId] = remaining
        return next
      })
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
        setOptimisticQueue((current) => {
          if (!Object.prototype.hasOwnProperty.call(current, runId)) return current
          const next = { ...current }
          delete next[runId]
          return next
        })
        setStreamingRuns({})
        setPendingApprovals([])
        setRunFacts(emptyRunFacts)
        setScheduler(emptyScheduler)
        setError('会话已不存在或已被其他窗口清理，请从左侧重新选择会话。')
        return
      }
      setError(displayApiError(reason))
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
    if (finalizedStreamSegmentsRef.current.has(`${next.run_id}:${next.segment ?? 0}`)) return
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
    setSseStatus(activeSession ? 'reconnecting' : 'connected')
    setSseNotice(null)
    sseHealthReasonRef.current = null
    sseRetryCount.current = 0
    continuationGeneration.current += 1
    setContext(emptyContext)
    setRunFacts(emptyRunFacts)
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
    const markSseHealthy = (durableSignal = false) => {
      if (sseHealthReasonRef.current === 'sse_reconcile_error' && !durableSignal) return
      sseHealthReasonRef.current = null
      setSseStatus('connected')
      setSseNotice(null)
    }
    source.onopen = () => {
      const reconnected = streamOpened.current
      streamOpened.current = true
      sseRetryCount.current = 0
      markSseHealthy()
      // Reconcile the durable cursor after every reconnect.  Ephemeral chunks
      // are deliberately not replayed as if they were committed messages. The
      // initial open already overlaps the effect's loadSession request.
      if (reconnected) scheduleSessionRefresh(streamSession)
    }
    source.addEventListener('stream', (message) => {
      try {
        const next = JSON.parse((message as MessageEvent).data) as StreamItem
        markSseHealthy()
        if (!next.run_id) return
        enqueueStreamItem(streamSession, next)
      } catch {
        setError('实时流式数据格式不可用，已等待下一条消息。')
      }
    })
    source.addEventListener('heartbeat', () => markSseHealthy(true))
    source.addEventListener('stream_error', (message) => {
      try {
        const details = JSON.parse((message as MessageEvent).data) as StreamErrorDetail
        if (details.code === 'sse_reconcile_error') {
          sseHealthReasonRef.current = 'sse_reconcile_error'
        }
        if (details.code === 'session_not_found') {
          source.close()
          void loadSession(streamSession)
          return
        }
        const notice = details.code === 'sse_reconcile_error'
          ? details.message || SSE_COPY.reconcileError
          : details.message || SSE_COPY.reconnecting
        setSseStatus('reconnecting')
        setSseNotice(notice)
        scheduleSessionRefresh(streamSession)
      } catch {
        setSseStatus('reconnecting')
        setSseNotice(SSE_COPY.reconnecting)
      }
    })
    source.addEventListener('runtime', (message) => {
      try {
        const next = JSON.parse((message as MessageEvent).data) as EventItem
        markSseHealthy(true)
        enqueueRuntimeEvent(streamSession, next)
        if (
          next.event_type === 'ActionSucceeded'
          && Array.isArray(next.payload?.modified_paths)
          && next.payload.modified_paths.length > 0
        ) {
          const firstChangedPath = next.payload.modified_paths.find((path): path is string => typeof path === 'string')
          if (firstChangedPath) setArtifactTargetPath(firstChangedPath.replace(/\\/g, '/'))
          setArtifactRefreshKey((value) => value + 1)
          setRightPane('artifacts')
          setRightCollapsed(false)
        }
        // Goal/plan/todo facts are root-run projections. Child Runtime events
        // continue to appear in the activity stream but must not replace the
        // parent contract shown by the status-circle inspector.
        if (next.run_id === streamSession) setRunFacts((current) => reduceRunFacts(current, next))
        const terminalStreamEvent = ['RunOutcomeRecorded', 'RunStalled', 'RunCancelled', 'RunFailed'].includes(next.event_type)
        if (['AssistantMessageCommitted', 'RunOutcomeRecorded', 'RunStalled', 'RunCancelled', 'RunFailed'].includes(next.event_type)) {
          if (next.event_type === 'AssistantMessageCommitted') {
            const segment = Number(next.payload?.segment)
            if (Number.isFinite(segment)) finalizedStreamSegmentsRef.current.add(`${next.run_id}:${segment}`)
          }
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
              if (state?.reasoning) {
                nextState[runKey] = {
                  ...state,
                  text: '',
                  phase: 'done',
                  reasoning_done: true,
                  reasoning_seconds: state.reasoning_seconds ?? (state.reasoning_started_at
                    ? Math.max(1, Math.round((Date.now() - state.reasoning_started_at) / 1000))
                    : undefined),
                  frozen: true,
                }
              } else {
                delete nextState[runKey]
              }
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
      // EventSource retries with Last-Event-ID automatically.  A browser
      // disconnect is non-blocking: keep the partial composer text and let
      // the durable timeline reconcile it once the connection opens again.
      // Live chunks are ephemeral, so freeze the visible prefix below instead
      // of appending across a gap that EventSource cannot replay.
      const attempt = ++sseRetryCount.current
      setSseStatus(attempt >= SSE_STALE_RETRY_THRESHOLD ? 'stale' : 'reconnecting')
      setSseNotice(attempt >= SSE_STALE_RETRY_THRESHOLD
        ? SSE_COPY.stale
        : SSE_COPY.reconnectingAfterDisconnect)
      setStreamingRuns((current) => {
        let changed = false
        const next = { ...current }
        for (const [runId, state] of Object.entries(current)) {
          if (
            (state.root_run_id === streamSession || state.run_id === streamSession)
            && ['content', 'reasoning', 'tool'].includes(state.phase)
            && !state.frozen
          ) {
            next[runId] = { ...state, phase: 'gap', frozen: true, updated_at: Date.now() }
            changed = true
          }
        }
        return changed ? next : current
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
    if (!activeStreaming || (!activeStreaming.text && !activeStreaming.reasoning)) return null
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
      stream_state: activeStreaming,
    }
  }, [activeStreaming])
  const displayEvents = useMemo(
    () => {
      let authoritative = events
      if (streamingEvent) {
        const timestamp = eventTime(streamingEvent)
        let index = events.findIndex((event) => eventTime(event) > timestamp)
        if (index < 0) index = events.length
        authoritative = [...events]
        authoritative.splice(index, 0, streamingEvent)
      }
      return mergeEventLists(
        authoritative,
        activeSession ? (optimisticEvents[activeSession] ?? []) : [],
      )
    },
    [activeSession, events, optimisticEvents, streamingEvent],
  )

  useEffect(() => {
    if (displayEvents.length === 0) return
    if (!stickToBottom.current) {
      setShowNewMessages(true)
      return
    }
    window.requestAnimationFrame(() => scrollConversationToLatest(activeStreaming ? 'auto' : 'smooth'))
  }, [activeStreaming, displayEvents.length])

  function scrollConversationToLatest(behavior: ScrollBehavior) {
    const element = conversationRef.current
    if (!element) return
    const distance = element.scrollHeight - element.clientHeight - element.scrollTop
    // A smooth scroll across a multi-thousand-turn virtual history can take
    // minutes and feels like a broken jump control. Keep short movements
    // animated, but place the reader immediately when the gap is large.
    const effectiveBehavior = behavior === 'smooth' && distance > Math.max(1600, element.clientHeight * 2)
      ? 'instant'
      : behavior
    element.scrollTo({ top: element.scrollHeight, behavior: effectiveBehavior })
  }

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
    scrollConversationToLatest('smooth')
  }

  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey
      if (modifier && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        searchRef.current?.focus()
      }
      if (modifier && event.key.toLowerCase() === 'n') {
        event.preventDefault()
        startNewSession()
      }
      if (modifier && (event.key === '/' || event.key === '?')) {
        event.preventDefault()
        setShortcutsOpen(true)
      }
      if (modifier && event.key === ',') {
        event.preventDefault()
        setSettingsOpen(true)
      }
      if (event.key === 'Escape' && shortcutsOpen) setShortcutsOpen(false)
      if (event.key === 'Escape' && contextOpen) {
        setContextOpen(false)
        setContextAnchor(null)
      }
    }
    window.addEventListener('keydown', onShortcut)
    return () => window.removeEventListener('keydown', onShortcut)
  }, [contextOpen, shortcutsOpen])

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
    return [...groups.entries()].map(([root, groupSessions]) => ({
      root,
      sessions: [...groupSessions].sort((left, right) => (
        Date.parse(right.updated_at ?? right.created_at ?? '') || 0
      ) - (
        Date.parse(left.updated_at ?? left.created_at ?? '') || 0
      )),
    }))
  }, [filteredSessions, project, sessionQuery])
  const conversationEvents = useMemo(
    () => displayEvents.filter((event) => event.kind === 'user' || event.kind === 'assistant' || event.kind === 'tool' || toolLifecycleTypes.has(event.event_type)),
    [displayEvents],
  )
  const conversationTurns = useMemo(() => groupConversationTurns(conversationEvents), [conversationEvents])
  const retryableTurn = useMemo(() => {
    if (!active || !['cancelled', 'stalled', 'failed_recoverable', 'blocked'].includes(active.status)) return null
    let lastUserIndex = -1
    conversationEvents.forEach((event, index) => {
      if (event.kind === 'user' && Boolean(event.content?.trim())) lastUserIndex = index
    })
    if (lastUserIndex < 0) return null
    const laterEvents = conversationEvents.slice(lastUserIndex + 1)
    const mayHaveExecutedAction = laterEvents.some((event) => [
      'ActionStarted', 'ActionSucceeded', 'ActionFailed', 'ToolObservationCommitted', 'ApprovalDecided',
    ].includes(event.event_type))
    if (mayHaveExecutedAction) return null
    const lastUser = conversationEvents[lastUserIndex]
    return lastUser.content ? { text: lastUser.content, id: lastUser.id } : null
  }, [active, conversationEvents])
  const conversationVirtualizer = useVirtualizer({
    count: conversationTurns.length,
    getScrollElement: () => conversationRef.current,
    estimateSize: () => 360,
    getItemKey: (index) => conversationTurns[index]?.id ?? index,
    overscan: 6,
  })
  const durableQueue = useMemo(() => deriveQueuedMessages(displayEvents), [displayEvents])
  const durableFollowUpIds = useMemo(
    () => new Set(displayEvents.filter((event) => event.event_type === 'FollowUpQueued').map((event) => String(event.payload?.follow_up_run_id ?? '')).filter(Boolean)),
    [displayEvents],
  )
  const queueItems = useMemo(() => {
    const optimistic = activeSession ? optimisticQueue[activeSession] ?? [] : []
    if (optimistic.length === 0) return durableQueue
    const durableContent = new Set(durableQueue.map((item) => item.content))
    return [
      ...optimistic.filter((item) => !durableFollowUpIds.has(item.follow_up_run_id) && !(item.optimistic && item.follow_up_run_id.startsWith('pending-') && durableContent.has(item.content))),
      ...durableQueue,
    ]
  }, [activeSession, durableFollowUpIds, durableQueue, optimisticQueue])
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

  async function openArtifactFile(path: string, projectRoot: string) {
    await webApi<{ opened: boolean }>('/api/artifacts/open', {
      method: 'POST',
      body: JSON.stringify({ path, project_root: projectRoot }),
    })
    notify('已使用本机默认程序打开文件')
  }

  function continueToolResult(observationId: string, cursor: string) {
    if (!observationId || !cursor || observationId.length > 128 || cursor.length > 128) return
    const prompt = `请调用 ContinueToolResult 继续读取这个只读结果，参数 observation_id=${JSON.stringify(observationId)}、next_cursor=${JSON.stringify(cursor)}。不要重新执行原工具，也不要修改文件。`
    void sendMessage(prompt)
  }

  function canContinueToolResult(observation: EventItem) {
    if (!active || !['cancelled', 'stalled', 'failed_recoverable', 'blocked'].includes(active.status)) return false
    if (observation.run_id !== active.run_id) return false
    // Follow-up messages run in a child Run. ContinueToolResult deliberately
    // reads only observations committed by its own Run, so an older cursor
    // cannot be offered after the conversation has moved to a child.
    return !displayEvents.some((event) => event.event_type === 'FollowUpQueued'
      && event.run_id === observation.run_id
      && event.sequence > observation.sequence)
  }

  function inspectArtifactPath(path: string) {
    const relative = path.replace(/\\/g, '/').replace(/^\.\//, '')
    if (!relative || relative.startsWith('/') || /^[a-z]:\//i.test(relative) || relative.split('/').includes('..')) {
      notify('该路径不属于当前项目文件列表')
      return
    }
    setArtifactTargetPath(relative)
    setMainPage('chat')
    setRightPane('artifacts')
    setRightCollapsed(false)
    setArtifactRefreshKey((value) => value + 1)
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

  function requestDeleteSession(session: Session) {
    if (deletingSession) return
    setError(null)
    setDeleteCandidate((current) => current === session.run_id ? null : session.run_id)
  }

  async function activateSession(session: Session) {
    const generation = ++selectionGeneration.current
    const previousSession = activeSessionRef.current
    const previousDraftKey = draftKeyRef.current
    const sameProject = !session.project_root || project?.root === session.project_root
    setError(null)
    setDeleteCandidate(null)
    setMainPage('chat')

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
      setError(displayApiError(reason))
    } finally {
      if (selectionGeneration.current === generation) selectionAbort.current = null
    }
  }

  async function chooseProject() {
    try {
      const result = await webApi<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ picker: true }) })
      if (result.selected) {
        setMainPage('chat')
        setProject(result.project)
        if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
        setSessions(visibleSessions(result.sessions))
        switchDraft(draftKeyForSession(null, result.project?.root))
        setActiveSession(null)
        setEvents([])
        setOptimisticEvents({})
        setRunFacts(emptyRunFacts)
        setError(null)
        notify('已切换到 ' + (result.project?.root ?? '本地项目'))
      }
    } catch (reason) {
      setError(displayApiError(reason))
    }
  }

  async function chooseProjectManually() {
    const path = window.prompt('输入本机项目文件夹的完整路径')
    if (!path) return
    try {
      const result = await webApi<{ selected: boolean; project: { root: string } | null; settings?: SettingsState; sessions: Session[] }>('/api/projects/select', { method: 'POST', body: JSON.stringify({ path }) })
      if (result.selected) {
        setMainPage('chat')
        setProject(result.project)
        if (result.settings) setSettings({ ...result.settings, permission_mode: normalizePermissionMode(result.settings.permission_mode) })
        setSessions(visibleSessions(result.sessions))
        switchDraft(draftKeyForSession(null, result.project?.root))
        setActiveSession(null)
        setEvents([])
        setOptimisticEvents({})
        setRunFacts(emptyRunFacts)
        setError(null)
        notify('已选择项目')
      }
    } catch (reason) {
      setError(displayApiError(reason))
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
      setError(displayApiError(reason))
    }
  }

  async function sendMessage(retryText?: string) {
    if (!project) {
      setError('请先选择本地项目文件夹')
      notify('选择项目后才能开始工作')
      return
    }
    const text = (retryText ?? draftRef.current).trim()
    if (!text || sending) return
    const requestedSession = activeSession
    // A stalled/cancelled run may be resumed in-place. Allow its next
    // generation to stream again instead of treating the old terminal event
    // as a permanent tombstone for the run ID.
    if (requestedSession) {
      finalizedStreamRunsRef.current.clear()
      finalizedStreamSegmentsRef.current.clear()
    }
    setSending(true)
    setError(null)
    setStreamingRuns({})
    try {
      const result = await webApi<{ session_id: string; sequence?: number; message_event_id?: string; follow_up_run_id?: string | null; continuation?: 'same_run' | 'child_run' }>('/api/messages', { method: 'POST', body: JSON.stringify({ text, session_id: requestedSession, project_root: project?.root ?? null }) })
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
      if (requestedSession && result.continuation === 'child_run') {
        const followUpId = result.follow_up_run_id ?? 'pending-' + optimisticEvent.id
        setOptimisticQueue((current) => ({
          ...current,
          [sessionId]: [
            ...(current[sessionId] ?? []),
            {
              follow_up_run_id: followUpId,
              predecessor_run_id: sessionId,
              gate: pendingApprovals.length > 0 ? 'approval' : 'waiting',
              status: 'queued',
              queued_sequence: result.sequence ?? 0,
              content: text,
              optimistic: true,
            },
          ],
        }))
      }
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
      setError(displayApiError(reason))
    } finally {
      setSending(false)
    }
  }

  async function stopSession() {
    if (!activeSession) return
    const runId = activeSession
    setStreamingRuns((current) => {
      const state = current[runId]
      if (!state) return current
      return {
        ...current,
        [runId]: { ...state, phase: 'stopped', frozen: true, updated_at: Date.now() },
      }
    })
    try {
      await webApi('/api/sessions/' + encodeURIComponent(runId) + '/stop', { method: 'POST' })
      await loadSession(runId)
      const list = await webApi<{ sessions: Session[] }>('/api/sessions')
      setSessions(visibleSessions(list.sessions))
      notify('已保存停止边界；可以输入新指令转向')
    } catch (reason) {
      setError(displayApiError(reason))
    }
  }

  async function deleteSession(session: Session) {
    if (deletingSession) return
    const runId = session.run_id
    setDeleteCandidate(null)
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
      setOptimisticQueue((current) => {
        if (!Object.prototype.hasOwnProperty.call(current, runId)) return current
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
        setRunFacts(emptyRunFacts)
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
      setError(displayApiError(apiReason))
      // The request may have invalidated the selected session's load. Restore
      // the authoritative view after a recoverable delete conflict.
      if (activeSessionRef.current === runId) void loadSession(runId)
    } finally {
      setDeletingSession(null)
    }
  }

  async function resumeSession() {
    if (!activeSession) return
    const runId = activeSession
    const resumableStream = Object.fromEntries(
      Object.entries(streamingRuns).filter(([, state]) => state.root_run_id === runId || state.run_id === runId),
    )
    finalizedStreamRunsRef.current.clear()
    finalizedStreamSegmentsRef.current.clear()
    if (streamBatchRef.current?.sessionId === runId) streamBatchRef.current = null
    if (streamFrameRef.current !== null) {
      window.cancelAnimationFrame(streamFrameRef.current)
      streamFrameRef.current = null
    }
    // A resumed worker may reuse the same run/segment identifiers and restart
    // its chunk counter at one. Drop the old frozen ephemeral prefix before
    // the resume command so its first new delta is not mistaken for a replay.
    setStreamingRuns((current) => Object.fromEntries(
      Object.entries(current).filter(([, state]) => state.root_run_id !== runId && state.run_id !== runId),
    ))
    try {
      await webApi('/api/sessions/' + encodeURIComponent(runId) + '/resume', { method: 'POST' })
      await loadSession(runId)
      notify('已从最近检查点继续')
    } catch (reason) {
      setStreamingRuns((current) => {
        const restore = Object.fromEntries(
          Object.entries(resumableStream).filter(([id]) =>
            !finalizedStreamRunsRef.current.has(id)
            && !Object.prototype.hasOwnProperty.call(current, id),
          ),
        )
        return { ...restore, ...current }
      })
      setError(displayApiError(reason))
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
        setError(displayApiError(reason))
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
        setError(displayApiError(reason))
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
      setMainPage('chat')
      setActiveSession(null)
      setEvents([])
      setStreamingRuns({})
      setOptimisticEvents({})
      setPendingApprovals([])
      setContext(emptyContext)
      setRunFacts(emptyRunFacts)
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
    setMainPage('chat')
    setDeleteCandidate(null)
    switchDraft(draftKeyForSession(null, project?.root))
    setActiveSession(null)
    setEvents([])
    setStreamingRuns({})
    setOptimisticEvents({})
    setPendingApprovals([])
    setContext(emptyContext)
    setRunFacts(emptyRunFacts)
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

  // Fresh sessions use the centered, quiet conversation stage. Existing
  // session/runtime facts continue to come from the same REST/SSE projections.
  const emptyState = !sessionLoading && !activeSession && conversationTurns.length === 0
  const shellClass = ['app-shell', leftCollapsed ? 'left-collapsed' : '', rightCollapsed ? 'right-collapsed' : ''].filter(Boolean).join(' ')
  const mainClass = ['main-panel', emptyState ? 'empty-state-panel' : '', emptyState && !project ? 'empty-no-project' : ''].filter(Boolean).join(' ')
  return (
    <div className={shellClass}>
      <aside className="left-sidebar">
        <div className="brand-row">
          <div className="brand-mark"><span className="brand-glyph">cc</span></div>
          <span className="brand-name">cc-harness</span>
          <span className="brand-live" title="本地 WebUI 已连接" />
          <button className="icon-button sidebar-menu" onClick={() => setLeftCollapsed(true)} title="收起侧栏"><PanelLeftClose size={16} /></button>
        </div>
        <div className="sidebar-rail-tools" aria-label="侧栏快捷操作">
          <button className="rail-tool rail-expand" onClick={() => setLeftCollapsed(false)} aria-label="展开侧栏" title="展开侧栏"><PanelLeftOpen size={17} /></button>
          <button className="rail-tool" onClick={startNewSession} aria-label="新建会话" title="新建会话"><MessageSquarePlus size={17} /></button>
          <button className="rail-tool" onClick={chooseProject} aria-label="选择工作区" title="选择工作区"><FolderOpen size={17} /></button>
          <button className="rail-tool" onClick={() => setMainPage('artifacts')} aria-label="查看产物" title="产物"><FileCode2 size={17} /></button>
          <button className="rail-tool" onClick={focusSidebarSearch} aria-label="搜索会话" title="搜索会话"><Search size={17} /></button>
          <button className="rail-tool" onClick={() => setSettingsOpen(true)} aria-label="自定义" title="自定义"><Settings size={17} /></button>
        </div>
        <nav className="app-navigation" aria-label="主导航">
          <button className="app-navigation-item" onClick={startNewSession}><MessageSquarePlus size={17} /><span>新建</span></button>
          <button className="app-navigation-item" onClick={chooseProject} title={project?.root ?? '选择本机项目'}><FolderOpen size={17} /><span>项目</span><small>{projectName}</small></button>
          <button className={'app-navigation-item' + (mainPage === 'artifacts' ? ' selected' : '')} onClick={() => setMainPage('artifacts')}><FileCode2 size={17} /><span>产物</span></button>
          <button className="app-navigation-item" onClick={() => setSettingsOpen(true)}><Settings size={17} /><span>自定义</span></button>
        </nav>
        <div className="session-heading"><div><span>聊天和任务</span><small>{sessions.length ? sessions.length + ' 个对话' : '暂无对话'}</small></div><div className="session-heading-actions"><button className="icon-button" onClick={focusSidebarSearch} title="搜索会话" aria-label="搜索会话"><Search size={16} /></button><button className="icon-button" onClick={startNewSession} title="新会话" aria-label="新建会话"><MessageSquarePlus size={17} /></button></div></div>
        <div className="session-search"><Search size={14} /><input ref={searchRef} value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} placeholder="搜索会话" aria-label="搜索会话" /><kbd>Ctrl K</kbd></div>
        <div className="session-list">
          {projectGroups.length === 0 && <div className="empty-sessions"><History size={18} /><p>{sessionQuery ? '没有匹配的会话' : '还没有会话'}</p><span>{sessionQuery ? '换个关键词试试' : '发送第一条任务开始工作'}</span></div>}
          {projectGroups.map(({ root, sessions: groupSessions }) => {
            const collapsed = collapsedProjects[root] ?? false
            const dateGroups = [
              { label: '今天', key: 'today' as const },
              { label: '近 7 天', key: 'week' as const },
              { label: '更早', key: 'older' as const },
            ].map((group) => ({
              ...group,
              sessions: groupSessions.filter((session) => sessionTimeGroup(session) === group.key),
            })).filter((group) => group.sessions.length > 0)
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
                  : dateGroups.map((dateGroup) => <section className="session-time-group" key={dateGroup.key}>
                    <h3>{dateGroup.label}</h3>
                    {dateGroup.sessions.map((session) => <div className="session-item-row" key={session.run_id}><button className={'session-item ' + (activeSession === session.run_id ? 'selected' : '')} onClick={() => void activateSession(session)}><StatusDot status={session.status} /><span className="session-title">{session.title}</span><span className="session-status">{statusLabels[session.status] ?? session.status}<span className="session-sequence"> · {session.sequence} 事件</span></span></button>{deleteCandidate === session.run_id ? <span className="session-delete-confirm" role="group" aria-label={'确认删除会话 ' + session.title}><span className="session-delete-confirm-label">删除？</span><button type="button" className="session-delete-confirm-yes" onClick={(event) => { event.stopPropagation(); void deleteSession(session) }}>确定</button><button type="button" className="session-delete-confirm-no" onClick={(event) => { event.stopPropagation(); setDeleteCandidate(null) }}>取消</button></span> : <button type="button" className="session-delete" onClick={(event) => { event.stopPropagation(); requestDeleteSession(session) }} disabled={deletingSession === session.run_id} aria-label={'删除会话 ' + session.title} title="删除会话">{deletingSession === session.run_id ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}</button>}</div>)}
                  </section>)}
              </div>}
            </section>
          })}
        </div>
        <div className="sidebar-bottom">
          <button className="project-chip" onClick={chooseProject}><FolderOpen size={15} /><span>{project ? project.root : '选择项目文件夹'}</span><ChevronRight size={14} /></button>
          <div className="sidebar-actions"><button className="settings-button" onClick={() => setSettingsOpen(true)} aria-label="设置"><Settings size={17} /><span>设置</span></button><button className="collapse-button" onClick={() => setLeftCollapsed(true)} title="收起侧栏" aria-label="收起侧栏"><PanelLeftClose size={16} /></button></div>
        </div>
      </aside>
      <main className={mainClass}>
        {mainPage === 'artifacts'
          ? <ProjectArtifacts key={project?.root ?? 'no-project'} projectRoot={project?.root ?? null} onChooseProject={() => void chooseProject()} onCopy={(text) => void copyText(text)} onOpen={openArtifactFile} initialPath={artifactTargetPath} reloadKey={artifactRefreshKey} />
          : <>
        <header className="topbar">
          <div className="breadcrumb"><span className="topbar-project">{projectName}</span><ChevronRight size={14} /><span className="topbar-title">{sessionLoading ? '正在载入会话…' : active ? active.title : '新会话'}</span></div>
          <div className="topbar-actions">{sseStatus !== 'connected' && <span className={'topbar-stream-state ' + sseStatus}><span className="sse-status-dot" />{sseStatus === 'stale' ? SSE_COPY.staleBadge : SSE_COPY.connectingBadge}</span>}<span className="topbar-live"><span className="pulse-dot" />本地 Runtime</span><button className="topbar-icon" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')} title={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}>{theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}</button><button className="topbar-icon" onClick={() => { setRightPane('artifacts'); setRightCollapsed(false) }} title="打开项目产物" aria-label="打开项目产物"><FileCode2 size={17} /></button><button className="topbar-icon" onClick={() => { if (rightCollapsed) { setRightPane('runtime'); setRightCollapsed(false) } else if (rightPane === 'runtime') { setRightCollapsed(true) } else { setRightPane('runtime') } }} title={rightCollapsed || rightPane === 'artifacts' ? '打开运行面板' : '收起运行面板'} aria-label="运行面板">{rightCollapsed ? <PanelRightOpen size={17} /> : <PanelRightClose size={17} />}</button><button className="topbar-icon" onClick={() => setSettingsOpen(true)} title="设置"><Settings size={16} /></button></div>
        </header>
              <section ref={conversationRef} className="conversation" aria-live="polite" onScroll={handleConversationScroll}>
            <div className="conversation-inner">
              {!project && <div className="welcome-state no-project"><div className="welcome-orbit"><Sparkles size={25} /></div><span className="welcome-kicker">本地工作空间</span><div className="welcome-title-line"><h1>把你的项目交给 cc-harness</h1></div><p>先选择一个本机项目文件夹，主 Agent 才能在安全边界内读取和修改代码。<br />浏览器关闭不会停止已提交的 Durable Run。</p><div className="welcome-actions"><button className="primary-button" onClick={chooseProject}><FolderOpen size={16} />选择项目文件夹</button><button className="text-button" onClick={chooseProjectManually}>手动输入路径</button></div><div className="welcome-note"><ShieldCheck size={14} />本地处理 · 可恢复检查点 · 可审计事件</div></div>}
              {project && sessionLoading && <div className="session-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={22} /><div><strong>正在载入会话</strong><span>正在从 Durable Runtime 对账事件、状态和检查点…</span></div></div>}
              {project && !sessionLoading && conversationTurns.length === 0 && <div className="welcome-state project-ready"><div className="welcome-orbit small"><Bot size={23} /></div><span className="welcome-kicker">项目已就绪 · {projectName}</span><div className="welcome-title-line"><h1>有什么可以帮你的？</h1></div><p>描述你希望完成的事，或从下面选一个开始。</p></div>}
              <div className="message-stack" style={{ height: conversationVirtualizer.getTotalSize() }}>
                {conversationVirtualizer.getVirtualItems().map((item) => {
                  const turn = conversationTurns[item.index]
                  if (!turn) return null
                  return <section
                    className="conversation-turn"
                    key={turn.id}
                    data-index={item.index}
                    ref={conversationVirtualizer.measureElement}
                    style={{ transform: `translateY(${item.start}px)` }}
                  >
                  {turn.user && <MessageCard event={turn.user} onCopy={copyText} />}
                  <TurnProcess
                    events={turn.process}
                    live={active != null && ['running', 'awaiting_approval'].includes(active.status) && item.index === conversationTurns.length - 1}
                    onCopy={copyText}
                    onOpenPath={inspectArtifactPath}
                    onContinueResult={continueToolResult}
                    canContinueResult={canContinueToolResult}
                  />
                  {turn.assistants.map((event) => <MessageCard event={event} key={event.id} onCopy={copyText} />)}
                  </section>
                })}
              </div>
              <div className="conversation-live-tail">
                {activeStreaming?.phase === 'tool' && <div className="live-tool-card" role="status"><LoaderCircle size={14} className="spin" /><span>正在调用 <strong>{activeStreaming.tool_name || '工具'}</strong></span></div>}
                {active && ['running', 'queued', 'awaiting_approval'].includes(active.status) && <div className="typing-indicator"><span /><span /><span /><em>{streamingHint(activeStreaming)}</em></div>}
                {activeStreaming?.error && <div className="stream-error-notice" role="status"><CircleAlert size={15} /><div><strong>{activeStreaming.error.message}</strong><span>{activeStreaming.error.next_action}</span></div></div>}
                {activeStreaming?.phase === 'stopped' && (activeStreaming.text || activeStreaming.reasoning) && <div className="event-notice"><CircleStop size={15} /><span>已停止，保留已生成的内容</span></div>}
                {activeStreaming?.phase === 'failed' && (activeStreaming.text || activeStreaming.reasoning) && <div className="event-notice error"><CircleAlert size={15} /><span>本轮失败，保留已生成的内容</span></div>}
                {retryableTurn && <div className="retry-turn-notice" role="status"><div><strong>本轮尚未执行工具，可以安全重试</strong><span>已保留原始消息；继续操作仍由 Runtime 检查点管理。</span></div><button className="secondary-button compact" onClick={() => void sendMessage(retryableTurn.text)}><RotateCcw size={14} />重试本轮</button></div>}
                {continuationNotice && <div className="continuation-notice" role="status"><LoaderCircle size={14} className="spin" /><span>{continuationNotice}</span></div>}
                {sseNotice && <div className={'continuation-notice sse-notice ' + sseStatus} role="status"><span className="sse-status-dot" />{sseNotice}</div>}
                {pendingApprovals.length > 0 && <ApprovalCard
                  approval={pendingApprovals[0]}
                  busy={approvalBusyId === pendingApprovals[0].approvalId}
                  toolArguments={displayEvents.find((event) => event.event_type === 'ActionPlanned' && String(event.payload?.action_id ?? '') === pendingApprovals[0].actionId)?.tool_arguments}
                  onApprove={() => void approve(pendingApprovals[0])}
                  onReject={() => void reject(pendingApprovals[0])}
                />}
                <div ref={bottomRef} />
              </div>
            </div>
            {showNewMessages && <button className="jump-to-latest" onClick={jumpToLatest}><ChevronDown size={14} />跳到最新消息</button>}
          </section>
        {error && <div className="error-banner"><AlertCircle size={16} /><span>{error}</span><button onClick={() => setError(null)} aria-label="关闭错误"><X size={14} /></button></div>}

        <footer className="composer-wrap">
          <div className="composer-label-row"><span>{sessionLoading ? '正在载入会话…' : active ? '继续与 cc-harness 协作' : '新的任务'}</span><span className="composer-shortcut"><Keyboard size={13} /> Enter 发送 · Shift+Enter 换行 · Ctrl+/ 快捷键</span></div>
          {active && <RuntimeProgress key={active.run_id} status={active.status} sequence={active.sequence} streaming={activeStreaming} events={displayEvents} hasApproval={pendingApprovals.length > 0} facts={runFacts} />}
          <QueuePanel items={queueItems} />
          <div className="composer">
            {commandPaletteOpen && <CommandPalette items={visibleCommands} selectedIndex={commandIndex} onSelect={selectCommand} />}
            <div className="composer-toolbar"><button className={'composer-project ' + (!project ? 'needs-project' : '')} onClick={chooseProject}><FolderOpen size={16} /><span>{project ? projectName : '选择项目文件夹'}</span><ChevronDown size={14} /></button><div className="composer-toolbar-right"><span className="composer-mode"><ShieldCheck size={13} />本地 Runtime</span></div></div>
             <textarea ref={textareaRef} value={draft} onChange={(event) => { updateDraft(event.target.value); setCursorPosition(event.currentTarget.selectionStart ?? event.currentTarget.value.length); setCommandIndex(0) }} onKeyDown={handleComposerKeyDown} onClick={(event) => { syncCursorPosition(event); if (!project) notify('请先选择本地项目文件夹') }} onKeyUp={syncCursorPosition} onSelect={syncCursorPosition} disabled={!project || sending || sessionLoading} placeholder={sessionLoading ? '正在载入会话…' : project ? '描述要完成的任务，或输入 / 查看命令…' : '请先选择本地项目文件夹'} rows={1} aria-label="任务输入框" />
          <div className="composer-footer"><div className="composer-footer-left"><PermissionSelector mode={settings.permission_mode} onChange={(mode) => void changePermissionMode(mode)} /><span className="privacy-note"><ShieldCheck size={13} />内容只在本机 Runtime 处理</span>{pendingApprovals.length > 0 && <span className="composer-queue-hint" title="这条消息会在当前工具审批完成后按顺序发送">审批后发送</span>}{active && ['running', 'queued', 'awaiting_approval'].includes(active.status) && pendingApprovals.length === 0 && <span className="composer-queue-hint">消息将排队</span>}</div><div className="composer-actions">{active && ['running', 'queued', 'awaiting_approval'].includes(active.status) && <button className="stop-button" onClick={() => void stopSession()} title="保存当前检查点并停止，随后可发送新指令" aria-label="停止运行"><CircleStop size={15} /><span>停止并转向</span></button>}{active && ['cancelled', 'stalled', 'failed_recoverable', 'blocked'].includes(active.status) && !retryableTurn && <button className="secondary-button compact" onClick={() => void resumeSession()}><RotateCcw size={15} />{active.status === 'blocked' ? '确认继续' : '继续'}</button>}<button className="send-button" onClick={() => void sendMessage()} disabled={!project || !draft.trim() || sending || sessionLoading} aria-label={sending ? '正在提交' : '发送'} title={sending ? '正在提交' : '发送'}>{sending ? <LoaderCircle size={16} className="spin" /> : <Send size={16} />}<span>{sending ? '提交中' : active && pendingApprovals.length > 0 ? '排队' : active && ['running', 'queued'].includes(active.status) ? '排队' : '发送'}</span></button></div></div>
          </div>
          {project && !sessionLoading && conversationTurns.length === 0 && <WelcomeSuggestions onChoose={usePrompt} />}
        </footer>
        <div className="bottom-statusbar">
          <div className="status-cluster"><span className="status-item"><StatusDot status={status} /><span>运行</span><strong>{status === 'idle' ? '待命' : statusLabels[status] ?? status}</strong></span><span className="status-divider" /><span className="status-item"><span className="status-check"><CircleCheck size={13} /></span><span>连接</span><strong className={executor.degraded ? 'status-warning' : 'status-ok'} title={executor.fallback_reason ?? undefined}>{connectionLabel}</strong></span><span className="status-divider" /><span className="status-item"><ShieldCheck size={13} /><span>权限</span><strong title={permissionModeSpecs.find((item) => item.mode === settings.permission_mode)?.description}>{permissionModeSpecs.find((item) => item.mode === settings.permission_mode)?.label ?? '请求批准'}</strong></span></div>
           <div className="bottom-status-actions"><div className="bottom-context-anchor"><ContextRing context={context} onClick={() => toggleContext('bottom')} compact />{contextOpen && contextAnchor === 'bottom' && <ContextPopover context={context} onClose={() => { setContextOpen(false); setContextAnchor(null) }} />}</div><button className="model-status" onClick={() => setSettingsOpen(true)}><span>模型</span><strong>{settings.model || '未配置'}</strong><ChevronDown size={14} /></button></div>
        </div>
          </>}
      </main>

      {!rightCollapsed && rightPane === 'runtime' && <div className="inspector-backdrop" onClick={() => setRightCollapsed(true)} aria-hidden="true" />}
      <aside className={'right-sidebar' + (rightPane === 'artifacts' ? ' artifact-inspector' : '')}>
        <div className="inspector-header">
          <div><span>{rightPane === 'artifacts' ? '项目产物' : '运行面板'}</span><small>{rightPane === 'artifacts' ? '文件由 Runtime 工具变更记录驱动' : '可观测状态'}</small></div>
          <div className="inspector-header-actions">
            <nav className="inspector-pane-tabs" aria-label="右侧面板" role="tablist">
              <button role="tab" aria-selected={rightPane === 'runtime'} className={rightPane === 'runtime' ? 'selected' : ''} onClick={() => setRightPane('runtime')}>运行</button>
              <button role="tab" aria-selected={rightPane === 'artifacts'} className={rightPane === 'artifacts' ? 'selected' : ''} onClick={() => setRightPane('artifacts')}>产物</button>
            </nav>
            <button className="icon-button" onClick={() => setRightCollapsed(true)} title="收起右侧面板" aria-label="收起右侧面板"><PanelRightClose size={16} /></button>
          </div>
        </div>
        {rightPane === 'artifacts'
          ? <ProjectArtifacts key={project?.root ?? 'no-project'} projectRoot={project?.root ?? null} onChooseProject={() => void chooseProject()} onCopy={(text) => void copyText(text)} onOpen={openArtifactFile} initialPath={artifactTargetPath} compact reloadKey={artifactRefreshKey} />
          : <>
        <div className="inspector-scroll">
           <section className="inspector-card runtime-overview"><div className="card-eyebrow"><span className="pulse-dot" /> DURABLE RUNTIME</div><div className="runtime-state"><StatusDot status={status} /><div><strong>{status === 'idle' ? '等待输入' : statusLabels[status] ?? status}</strong><span>{active ? '事件序号 ' + active.sequence : '选择项目后开始'}</span></div></div><div className="inspector-row"><span>项目</span><strong>{project ? '已选择' : '未选择'}</strong></div><div className="inspector-row"><span>活动会话</span><strong>{active ? '已连接' : '—'}</strong></div><div className="inspector-row"><span>执行后端</span><strong className={executor.degraded ? 'warn-text' : ''}>{connectionLabel}</strong></div><div className="inspector-row"><span>调度器</span><strong className={scheduler.mode === 'external' ? 'warn-text' : ''}>{scheduler.label}</strong></div><div className="inspector-row"><span>审批</span><strong className={pendingApprovals.length > 0 ? 'warn-text' : ''}>{pendingApprovals.length > 0 ? pendingApprovals.length + ' 项待处理' : '无待处理'}</strong></div></section>
           {unsupportedControls.length > 0 && <section className="inspector-card capability-note"><div className="card-eyebrow">当前运行时能力</div><p>以下控制由服务端标记为不可用：</p>{unsupportedControls.map(([id, feature]) => <div className="capability-row" key={id}><strong>{id}</strong><span>{feature.reason ?? '当前环境未提供'}</span></div>)}</section>}
           {diagnosis && <RuntimeDiagnosisCard diagnosis={diagnosis} />}
           {active && <RuntimeContextCard context={context} />}
           <section className="inspector-card live-status-card"><div className="card-heading"><div><span className="card-eyebrow">实时状态</span><small>{active ? active.title : '选择会话后显示'}</small></div><StatusDot status={status} /></div><div className="live-status-summary"><StatusDot status={status} /><div><strong>{status === 'idle' ? '等待输入' : statusLabels[status] ?? status}</strong><span>{active ? '事件序号 ' + active.sequence : '当前没有活动会话'}</span></div></div>{liveStatusEvents.length === 0 ? <div className="activity-empty"><Info size={15} /><span>任务运行后，这里会显示实时状态。</span></div> : <div className="runtime-timeline">{liveStatusEvents.map((event) => <div className={'runtime-timeline-row ' + event.kind} key={event.id}><span className="runtime-timeline-mark" /><div><strong>{runtimeEventLabel(event)}</strong><small>事件 #{event.sequence}</small></div></div>)}</div>}</section>
        </div>
        <div className="inspector-model"><div className="model-chip"><span className="model-chip-dot" /><div><small>当前模型</small><strong>{settings.model || '未配置'}</strong></div></div><button className="icon-button" onClick={() => setSettingsOpen(true)} title="设置"><Settings size={16} /></button></div>
          </>}
      </aside>
      {settingsOpen && <SettingsModal initial={settings} onClose={() => setSettingsOpen(false)} onSaved={(value) => setSettings(value)} />}
      {shortcutsOpen && <KeyboardShortcutsModal onClose={() => setShortcutsOpen(false)} />}
      {toast && <div className="toast" role="status"><Check size={15} />{toast}</div>}
    </div>
  )
}

export { App }

