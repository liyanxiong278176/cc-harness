/** Pure transcript / plan reducers shared by history replay and live SSE. */

import type {
  Approval, Plan, PlanNode, Projection, RuntimeEvent, TaskView, TodoItem,
} from './types'

export type FlowItem =
  | { kind: 'user'; id: string; content: string; time?: string; pending?: boolean; failed?: boolean }
  | { kind: 'assistant'; id: string; content: string; time?: string; streaming?: boolean }
  | { kind: 'reasoning'; id: string; content: string; streaming?: boolean }
  | {
    kind: 'tool'
    id: string
    actionId: string
    name: string
    label: string
    summary: string
    detail?: string
    arguments?: unknown
    result?: string
    status: 'running' | 'done' | 'error' | 'rejected'
    time?: string
  }
  | { kind: 'error'; id: string; content: string; time?: string }
  | { kind: 'notice'; id: string; content: string; time?: string }

type Json = Record<string, unknown>

const GENERATED_ID = /^(?:root|follow-up)-[\da-f-]{20,}$/i
const CONTROL_USER_TEXT = new Set(['WebUI 用户继续', 'webui.resume'])

function payloadOf(event: RuntimeEvent): Json {
  return event.payload && typeof event.payload === 'object' ? event.payload : {}
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

function asRecord(value: unknown): Json | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Json : undefined
}

function eventKey(event: RuntimeEvent): string {
  return event.event_id || event.id || `${event.run_id}:${event.sequence}:${event.event_type}`
}

export function mergeEvents(
  previous: RuntimeEvent[],
  incoming: RuntimeEvent[],
  replace = false,
): RuntimeEvent[] {
  if (replace) return dedupe(incoming)
  if (incoming.length === 0) return previous
  const merged = [...previous, ...incoming]
  return dedupe(merged)
}

function dedupe(events: RuntimeEvent[]): RuntimeEvent[] {
  const seen = new Set<string>()
  const unique: RuntimeEvent[] = []
  for (const event of events) {
    const key = eventKey(event)
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(event)
  }
  unique.sort((left, right) => {
    const a = Date.parse(left.occurred_at) || 0
    const b = Date.parse(right.occurred_at) || 0
    if (a !== b) return a - b
    if (left.run_id !== right.run_id) return left.run_id < right.run_id ? -1 : 1
    return left.sequence - right.sequence
  })
  return unique
}

export function maxRootSequence(events: RuntimeEvent[], runId: string): number {
  let value = 0
  for (const event of events) {
    if (event.run_id === runId && event.sequence > value) value = event.sequence
  }
  return value
}

export function toolLabel(name: string | undefined): string {
  const value = (name || '').trim()
  const normalized = value.toLowerCase()
  if (normalized.includes('run_command') || normalized === 'bash' || normalized === 'shell') return '运行命令'
  if (normalized === 'read' || normalized.includes('read_file')) return '读取文件'
  if (normalized === 'write' || normalized.includes('write_file')) return '写入文件'
  if (normalized === 'edit' || normalized.includes('edit')) return '编辑文件'
  if (normalized.includes('grep') || normalized.includes('search')) return '搜索'
  if (normalized.includes('glob') || normalized.includes('list')) return '查找文件'
  if (normalized.includes('todo') || normalized.includes('task_plan')) return '更新计划'
  if (normalized.includes('fetch') || normalized.includes('web')) return '访问网页'
  if (normalized.includes('image')) return '查看图片'
  return value || '工具调用'
}

export function toolSummary(event: RuntimeEvent): string {
  const args = asRecord(event.tool_arguments)
  if (args) {
    for (const key of ['command', 'path', 'file_path', 'pattern', 'query', 'url', 'description']) {
      const value = asString(args[key])
      if (value) return truncate(value.replace(/\s+/g, ' '), 96)
    }
  }
  const payload = payloadOf(event)
  for (const key of ['command', 'path', 'pattern', 'query', 'description']) {
    const value = asString(payload[key])
    if (value) return truncate(value.replace(/\s+/g, ' '), 96)
  }
  return ''
}

function truncate(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value
}

function errorText(event: RuntimeEvent): string {
  const payload = payloadOf(event)
  const direct = asString(event.content)
  if (direct) return direct
  const error = asRecord(event.error) ?? asRecord(payload.error) ?? asRecord(payload.error_details)
  if (error) {
    const message = asString(error.message)
    const code = asString(error.code)
    if (message) return code ? `${message} (${code})` : message
  }
  const reason = asString(payload.reason)
  if (reason) return reason
  return '执行失败'
}

/** Rebuild the display flow from the bounded durable event page. */
export function buildFlow(events: RuntimeEvent[]): FlowItem[] {
  const flow: FlowItem[] = []
  const tools = new Map<string, Extract<FlowItem, { kind: 'tool' }>>()

  const upsertTool = (
    actionId: string,
    event: RuntimeEvent,
    patch: Partial<Extract<FlowItem, { kind: 'tool' }>>,
  ): Extract<FlowItem, { kind: 'tool' }> => {
    const existing = tools.get(actionId)
    if (existing) {
      // Observation/start events carry no arguments.  Do not erase the
      // command/path summary captured from the matching ActionPlanned event.
      const next = { ...patch }
      if (!next.summary) delete next.summary
      if (!next.detail) delete next.detail
      Object.assign(existing, next)
      return existing
    }
    const item: Extract<FlowItem, { kind: 'tool' }> = {
      kind: 'tool',
      id: `tool:${actionId}`,
      actionId,
      name: patch.name ?? event.tool_name ?? '',
      label: patch.label ?? toolLabel(patch.name ?? event.tool_name),
      summary: patch.summary ?? toolSummary(event),
      status: patch.status ?? 'running',
      time: event.occurred_at,
      ...patch,
    }
    tools.set(actionId, item)
    flow.push(item)
    return item
  }

  for (const event of events) {
    const payload = payloadOf(event)
    switch (event.event_type) {
      case 'RunCreated':
      case 'FollowUpQueued':
      case 'TaskProgressRequested':
      case 'TaskGuidanceReceived':
      case 'RunResumed': {
        if (event.kind !== 'user') break
        const content = asString(event.content)
        if (!content || CONTROL_USER_TEXT.has(content.trim())) break
        const previous = flow[flow.length - 1]
        if (previous?.kind === 'user' && previous.content === content) break
        flow.push({ kind: 'user', id: eventKey(event), content, time: event.occurred_at })
        break
      }
      case 'TaskProgressReported':
      case 'AssistantMessageCommitted': {
        const content = asString(event.content)
        if (!content) break
        flow.push({ kind: 'assistant', id: eventKey(event), content, time: event.occurred_at })
        break
      }
      case 'ActionPlanned': {
        const actionId = asString(payload.action_id) ?? asString(event.action_id) ?? eventKey(event)
        const name = asString(payload.tool_name) ?? event.tool_name ?? ''
        upsertTool(actionId, event, {
          name,
          label: toolLabel(name),
          summary: toolSummary(event),
          arguments: event.tool_arguments,
          status: 'running',
        })
        break
      }
      case 'ActionStarted': {
        const actionId = asString(payload.action_id) ?? asString(event.action_id) ?? eventKey(event)
        const name = asString(payload.tool_name) ?? event.tool_name ?? ''
        upsertTool(actionId, event, { name, label: toolLabel(name), status: 'running' })
        break
      }
      case 'ToolObservationCommitted': {
        const actionId = asString(payload.action_id) ?? asString(event.action_id) ?? eventKey(event)
        const name = event.tool_name ?? asString(payload.tool_name) ?? ''
        const status = event.rejected || event.status === 'rejected'
          ? 'rejected'
          : event.status === 'failed' || event.status === 'error'
            ? 'error'
            : 'done'
        const result = asString(event.content) || ''
        upsertTool(actionId, event, {
          name,
          label: toolLabel(name),
          summary: toolSummary(event),
          status,
          result,
          detail: result ? truncate(result, 240) : undefined,
        })
        break
      }
      case 'ActionSucceeded': {
        const actionId = asString(payload.action_id) ?? asString(event.action_id) ?? eventKey(event)
        const existing = tools.get(actionId)
        if (existing) existing.status = 'done'
        break
      }
      case 'ActionFailed': {
        const actionId = asString(payload.action_id) ?? asString(event.action_id) ?? eventKey(event)
        const existing = tools.get(actionId)
        if (existing) {
          existing.status = 'error'
          existing.detail = errorText(event)
        }
        break
      }
      case 'ActionCancelled':
      case 'ActionOutcomeUnknown': {
        const actionId = asString(payload.action_id) ?? asString(event.action_id) ?? eventKey(event)
        const existing = tools.get(actionId)
        if (existing) {
          existing.status = 'error'
          existing.detail = event.event_type === 'ActionOutcomeUnknown' ? '执行结果未知' : '已取消'
        }
        break
      }
      case 'RunFailed':
      case 'ModelInvocationFinished': {
        if (event.kind !== 'error') break
        flow.push({ kind: 'error', id: eventKey(event), content: errorText(event), time: event.occurred_at })
        break
      }
      case 'RunBlocked': {
        const reason = asString(payload.reason) || '运行被阻塞'
        flow.push({ kind: 'error', id: eventKey(event), content: reason, time: event.occurred_at })
        break
      }
      case 'RunOutcomeRecorded': {
        const outcome = asRecord(payload.outcome) ?? asRecord(payload)
        const value = asString(outcome?.outcome)
        if (value && !['pass', 'completed', 'success'].includes(value)) {
          const reason = asString(asRecord(outcome?.details)?.reason) ?? value
          flow.push({ kind: 'notice', id: eventKey(event), content: `本轮结束：${reason}`, time: event.occurred_at })
        }
        break
      }
      default:
        break
    }
  }
  return flow
}

function todoFromPayload(payload: Json): TodoItem | null {
  const raw = asRecord(payload.todo)
  const todoId = asString(raw?.todo_id) ?? asString(raw?.id) ?? asString(payload.todo_id)
  if (!todoId) return null
  const title = asString(raw?.title) ?? todoId
  const status = asString(raw?.status) ?? 'pending'
  return {
    todo_id: todoId,
    title,
    status,
    plan_node_id: asString(raw?.plan_node_id),
    active_sessions: Array.isArray(raw?.active_sessions)
      ? raw.active_sessions.filter((value): value is string => typeof value === 'string')
      : [],
    updated_sequence: typeof raw?.updated_sequence === 'number' ? raw.updated_sequence : undefined,
    evidence: Array.isArray(raw?.evidence) ? raw.evidence : [],
  }
}

export function applyTodoEvent(todos: TodoItem[], event: RuntimeEvent): TodoItem[] {
  const payload = payloadOf(event)
  if (event.event_type === 'TodoCompleted') {
    const id = asString(payload.todo_id) ?? asString(asRecord(payload.todo)?.todo_id)
    if (!id) return todos
    return todos.map(todo => todo.todo_id === id ? { ...todo, status: 'completed' } : todo)
  }
  if (!['TodoCreated', 'TodoUpdated'].includes(event.event_type)) return todos
  const todo = todoFromPayload(payload)
  if (!todo) return todos
  const index = todos.findIndex(item => item.todo_id === todo.todo_id)
  if (index < 0) return [...todos, todo]
  const next = todos.slice()
  next[index] = { ...next[index], ...todo }
  return next
}

export function applyPlanEvent(plan: Plan | undefined, event: RuntimeEvent): Plan | undefined {
  const payload = payloadOf(event)
  if (event.event_type === 'PlanCreated' || event.event_type === 'PlanRevised') {
    const next = asRecord(payload.plan)
    if (!next) return plan
    const nodes = Array.isArray(next.nodes) ? next.nodes as PlanNode[] : plan?.nodes
    return {
      ...(plan ?? {}),
      ...next,
      nodes: nodes?.map(node => ({ ...node })),
      revision: typeof next.revision === 'number' ? next.revision : plan?.revision,
    }
  }
  if (!plan?.nodes) return plan
  const nodeId = asString(payload.node_id)
  if (!nodeId) return plan
  if (event.event_type === 'PlanNodeStarted' || event.event_type === 'PlanNodeCompleted') {
    const status = event.event_type === 'PlanNodeStarted' ? 'running' : 'completed'
    return {
      ...plan,
      nodes: plan.nodes.map(node => node.node_id === nodeId ? { ...node, status } : node),
    }
  }
  return plan
}

export function applyApprovalEvent(approvals: Approval[], event: RuntimeEvent): Approval[] {
  const payload = payloadOf(event)
  const id = asString(payload.approval_id)
  if (!id) return approvals
  if (event.event_type === 'ApprovalRequested') {
    if (approvals.some(item => item.approval_id === id)) return approvals
    return [...approvals, {
      approval_id: id,
      run_id: event.run_id,
      action_id: asString(payload.action_id),
      action_args_digest: asString(payload.action_args_digest),
      status: 'requested',
      scope: Array.isArray(payload.scope)
        ? payload.scope.filter((value): value is string => typeof value === 'string')
        : [],
      tool_name: asString(payload.tool_name),
    }]
  }
  if (event.event_type === 'ApprovalGranted' || event.event_type === 'ApprovalRejected') {
    return approvals.filter(item => item.approval_id !== id)
  }
  return approvals
}

export function statusFromEvent(current: string, event: RuntimeEvent): string {
  switch (event.event_type) {
    case 'RunCreated':
    case 'FollowUpQueued': return 'queued'
    case 'ActionPlanned':
    case 'ActionStarted':
    case 'AssistantMessageCommitted':
    case 'ToolObservationCommitted':
    case 'PlanNodeStarted': return 'running'
    case 'RunPaused': return 'paused'
    case 'RunBlocked': return 'blocked'
    case 'RunCancelled': return 'cancelled'
    case 'RunFailed': return 'failed_recoverable'
    case 'RunOutcomeRecorded': {
      const outcome = asRecord(payloadOf(event).outcome)
      const value = asString(outcome?.outcome)
      if (!value) return 'completed'
      if (['pass', 'completed', 'success'].includes(value)) return 'completed'
      if (value === 'blocked') return 'blocked'
      return 'paused'
    }
    default: return current
  }
}

function isGenerated(value: string): boolean {
  return GENERATED_ID.test(value)
}

function normalizeStatus(value: string | undefined): TaskView['status'] {
  if (value === 'completed' || value === 'done' || value === 'succeeded') return 'done'
  if (value === 'in_progress' || value === 'running' || value === 'active' || value === 'started') return 'running'
  if (value === 'blocked' || value === 'waiting_input' || value === 'waiting_approval') return 'blocked'
  if (value === 'failed' || value === 'error') return 'failed'
  return 'pending'
}

/**
 * The runtime emits one generated todo per Run plus, when the agent uses the
 * planning tools, real user-facing steps.  Prefer the real todos; fall back to
 * titled plan nodes so a plan-first session still gets a visible checklist.
 */
export function deriveTasks(
  todos: TodoItem[],
  plan: Plan | undefined,
  runStatus: string,
  activeRunId?: string,
): TaskView[] {
  const useful = todos.filter(todo => !isGenerated(todo.todo_id) && !isGenerated(todo.title))
  if (useful.length > 0) {
    return useful.map((todo, index) => ({
      id: todo.todo_id || `todo-${index}`,
      title: todo.title,
      status: normalizeStatus(todo.status),
    }))
  }
  const nodes = plan?.nodes ?? []
  const titled = nodes.filter(node => {
    const title = asString(node.title)
    if (title && !isGenerated(title)) return true
    return false
  })
  const selected = titled.length > 0 ? titled : nodes.filter(node => node.kind !== 'root' && node.kind !== 'follow_up')
  return selected.slice(0, 40).map((node, index) => {
    const rawTitle = asString(node.title)
    const criterion = Array.isArray(node.acceptance_criteria)
      ? asString(node.acceptance_criteria[0])
      : undefined
    const title = rawTitle && !isGenerated(rawTitle)
      ? rawTitle
      : criterion
        ? truncate(criterion, 96)
        : `步骤 ${index + 1}`
    const todo = todos.find(item => item.plan_node_id === node.node_id)
    const status = node.status
      ? normalizeStatus(node.status)
      : todo
        ? normalizeStatus(todo.status)
        : activeRunId && node.child_run_id === activeRunId
          ? 'running'
          : 'pending'
    return { id: node.node_id, title, status }
  })
}

export function mergeTodoLists(base: TodoItem[], addition: TodoItem[]): TodoItem[] {
  if (addition.length === 0) return base
  const byId = new Map(base.map(todo => [todo.todo_id, todo]))
  for (const todo of addition) {
    const existing = byId.get(todo.todo_id)
    // `base` is the authoritative snapshot; the extra list only supplies
    // items that fell outside the bounded page, never stale statuses.
    byId.set(todo.todo_id, existing ? { ...todo, ...existing } : todo)
  }
  return [...byId.values()]
}

export function projectionTodos(projection: Projection | undefined): TodoItem[] {
  const value = projection?.todos
  return Array.isArray(value) ? value : []
}

export function projectionPlan(projection: Projection | undefined): Plan | undefined {
  return projection?.plan
}

export function projectionApprovals(projection: Projection | undefined): Approval[] {
  const value = projection?.approvals
  return Array.isArray(value) ? value.filter(item => item.status === 'requested' || !item.status) : []
}

export function projectionQueue(projection: Projection | undefined): string[] {
  const value = projection?.queue
  if (!Array.isArray(value)) return []
  return value
    .filter(item => item.status === 'queued' || item.status === 'waiting')
    .map(item => item.follow_up_run_id)
}
