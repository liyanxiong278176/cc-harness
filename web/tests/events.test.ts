import assert from 'node:assert/strict'
import test from 'node:test'
import {
  applyApprovalEvent, applyPlanEvent, applyTodoEvent, buildFlow, deriveTasks,
  mergeEvents, mergeTodoLists, statusFromEvent, type FlowItem,
} from '../src/app/events.ts'
import type { RuntimeEvent, TodoItem } from '../src/app/types.ts'

let sequence = 0
function event(
  eventType: string,
  kind: RuntimeEvent['kind'],
  payload: Record<string, unknown> = {},
  content?: string,
): RuntimeEvent {
  sequence += 1
  return {
    id: `run:${sequence}`,
    event_id: `event-${sequence}`,
    run_id: 'run',
    sequence,
    event_type: eventType,
    occurred_at: new Date(1_700_000_000_000 + sequence * 1000).toISOString(),
    kind,
    payload,
    content,
  }
}

test('mergeEvents dedupes and orders by occurrence', () => {
  const first = event('AssistantMessageCommitted', 'assistant', {}, 'one')
  const second = event('AssistantMessageCommitted', 'assistant', {}, 'two')
  const merged = mergeEvents([second], [first, second])
  assert.deepEqual(merged.map(item => item.content), ['one', 'two'])
})

test('buildFlow hides control continuations and folds one tool per action', () => {
  const flow = buildFlow([
    event('RunCreated', 'user', {}, 'hello'),
    event('TaskGuidanceReceived', 'user', {}, 'WebUI 用户继续'),
    event('ActionPlanned', 'status', {
      action_id: 'call-1', tool_name: 'run_command',
    }, undefined),
    event('ToolObservationCommitted', 'tool', {
      action_id: 'call-1', tool_name: 'run_command', status: 'succeeded',
    }, 'ok'),
  ])
  assert.equal(flow.filter(item => item.kind === 'user').length, 1)
  const tools = flow.filter((item): item is Extract<FlowItem, { kind: 'tool' }> => item.kind === 'tool')
  assert.equal(tools.length, 1)
  assert.equal(tools[0]?.status, 'done')
  assert.equal(tools[0]?.result, 'ok')
})

test('tool summaries survive a later observation event', () => {
  const flow = buildFlow([
    event('ActionPlanned', 'status', {
      action_id: 'call-2', tool_name: 'run_command',
    }, undefined),
    event('ToolObservationCommitted', 'tool', {
      action_id: 'call-2', tool_name: 'run_command', status: 'succeeded',
    }, 'done'),
  ])
  const tool = flow.find(item => item.kind === 'tool') as Extract<FlowItem, { kind: 'tool' }>
  assert.equal(tool.label, '运行命令')
  assert.equal(tool.status, 'done')
})

test('deriveTasks mirrors the Runtime todo list, including generated Run ids', () => {
  const generated: TodoItem = {
    todo_id: 'follow-up-0123456789abcdef0123456789abcdef',
    title: '回答用户身份问题',
    status: 'done',
  }
  const real: TodoItem = { todo_id: 'step1', title: '检查目录', status: 'in_progress' }
  assert.deepEqual(deriveTasks([generated, real], undefined, 'running'), [
    { id: generated.todo_id, title: '回答用户身份问题', status: 'done' },
    { id: 'step1', title: '检查目录', status: 'running' },
  ])
  // The plan-node fallback is only for a Runtime that has not materialised a
  // todo list yet.
  assert.deepEqual(deriveTasks([], {
    nodes: [{ node_id: 'step-one', title: '检查目录' }, { node_id: 'step-two', title: '汇报' }],
  }, 'running'), [
    { id: 'step-one', title: '检查目录', status: 'pending' },
    { id: 'step-two', title: '汇报', status: 'pending' },
  ])
})

test('todo, plan and approval events update without dropping prior state', () => {
  let todos: TodoItem[] = []
  todos = applyTodoEvent(todos, event('TodoCreated', 'status', {
    todo: { id: 'step1', title: '检查目录', status: 'pending' },
  }))
  todos = applyTodoEvent(todos, event('TodoUpdated', 'status', {
    todo: { id: 'step1', title: '检查目录', status: 'in_progress' },
  }))
  todos = applyTodoEvent(todos, event('TodoCompleted', 'status', { todo_id: 'step1' }))
  assert.equal(todos[0]?.status, 'completed')

  const plan = applyPlanEvent(undefined, event('PlanRevised', 'status', {
    plan: { revision: 2, nodes: [{ node_id: 'step1', title: '检查目录' }] },
  }))
  assert.equal(plan?.revision, 2)
  const started = applyPlanEvent(plan, event('PlanNodeStarted', 'status', { node_id: 'step1' }))
  assert.equal(started?.nodes?.[0]?.status, 'running')

  const approvals = applyApprovalEvent([], event('ApprovalRequested', 'status', {
    approval_id: 'approval-1', action_id: 'a1', action_args_digest: 'digest', scope: ['run_command'],
  }))
  assert.equal(approvals.length, 1)
  const decided = applyApprovalEvent(approvals, event('ApprovalGranted', 'status', { approval_id: 'approval-1' }))
  assert.equal(decided.length, 0)
})

test('TodoUpdated keeps plan_node_id while applying task_plan title and status', () => {
  let todos: TodoItem[] = []
  todos = applyTodoEvent(todos, event('TodoCreated', 'status', {
    todo: {
      id: 'follow-up-run',
      plan_node_id: 'follow-up-run',
      title: '你是谁',
      status: 'pending',
    },
  }))
  todos = applyTodoEvent(todos, event('TodoUpdated', 'status', {
    todo: { id: 'follow-up-run', title: '回答用户身份问题', status: 'done' },
  }))
  assert.equal(todos[0]?.title, '回答用户身份问题')
  assert.equal(todos[0]?.status, 'done')
  assert.equal(todos[0]?.plan_node_id, 'follow-up-run')
  assert.deepEqual(deriveTasks(todos, undefined, 'completed'), [
    { id: 'follow-up-run', title: '回答用户身份问题', status: 'done' },
  ])
})

test('mergeTodoLists keeps snapshot fields authoritative', () => {
  const merged = mergeTodoLists(
    [{ todo_id: 'a', title: 'new', status: 'done' }],
    [{ todo_id: 'a', title: 'old', status: 'pending' }, { todo_id: 'b', title: 'extra', status: 'pending' }],
  )
  assert.equal(merged.find(item => item.todo_id === 'a')?.status, 'done')
  assert.equal(merged.find(item => item.todo_id === 'b')?.title, 'extra')
})

test('statusFromEvent maps durable terminal events', () => {
  assert.equal(statusFromEvent('running', event('RunOutcomeRecorded', 'outcome', {
    outcome: { outcome: 'pass' },
  })), 'completed')
  assert.equal(statusFromEvent('running', event('RunBlocked', 'status', { reason: 'x' })), 'blocked')
  assert.equal(statusFromEvent('running', event('RunCancelled', 'status')), 'cancelled')
})
