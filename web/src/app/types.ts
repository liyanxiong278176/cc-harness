/** Wire types shared by the rebuilt DeepSeek-Harness-style WebUI. */

export type Session = {
  run_id: string
  title: string
  status: string
  sequence: number
  active_worker_id?: string | null
  updated_sequence?: number
  created_at?: string | null
  updated_at?: string | null
  project_root?: string
  scheduler?: { mode?: string; running?: boolean; label?: string }
}

export type Settings = {
  base_url: string
  model: string
  permission_mode: string
  has_api_key?: boolean
  api_key_masked?: string
}

export type Bootstrap = {
  service: string
  runtime: string
  project: { root: string } | null
  settings: Settings
  sessions: Session[]
  initial_prompt?: string | null
}

export type Actor = { kind?: string; id?: string }

export type RuntimeEvent = {
  id: string
  event_id?: string
  run_id: string
  sequence: number
  event_type: string
  occurred_at: string
  actor?: Actor
  payload?: Record<string, unknown>
  kind: 'user' | 'assistant' | 'tool' | 'status' | 'error' | 'outcome'
  content?: string
  tool_name?: string
  action_id?: string
  status?: string
  rejected?: boolean
  tool_arguments?: unknown
  exit_code?: number
  duration_ms?: number
  read_paths?: string[]
  modified_paths?: string[]
  result_lines_total?: number
  result_lines_shown?: number
  result_truncated?: boolean
  complete?: boolean
  error?: { message?: string; code?: string; phase?: string }
  outcome?: Record<string, unknown>
}

export type TodoItem = {
  todo_id: string
  title: string
  status: string
  plan_node_id?: string | null
  active_sessions?: string[]
  updated_sequence?: number
  evidence?: unknown[]
}

export type PlanNode = {
  node_id: string
  kind?: string
  title?: string
  status?: string
  depth?: number
  depends_on?: string[]
  acceptance_criteria?: string[]
  effect_class?: string
  child_run_id?: string | null
  required?: boolean
}

export type Plan = {
  nodes?: PlanNode[]
  revision?: number
}

export type Approval = {
  approval_id: string
  run_id?: string
  action_id?: string
  action_args_digest?: string
  status?: string
  scope?: string[]
  tool_name?: string
  summary?: string
}

export type QueueItem = {
  follow_up_run_id: string
  predecessor_run_id?: string
  message_artifact?: string
  gate?: string
  status?: string
  queued_sequence?: number
  continuation?: boolean
}

export type Projection = {
  run_id?: string
  status?: string
  plan?: Plan
  todos?: TodoItem[]
  approvals?: Approval[]
  queue?: QueueItem[]
  goal?: { objective?: string }
  task?: Record<string, unknown>
  effective_run_id?: string
  [key: string]: unknown
}

export type ChatSnapshot = {
  run_id: string
  project_root: string
  status: string
  task_run_id: string
  task_projection?: Projection
  task?: Record<string, unknown>
  plan_digest?: string
  sequence: number
  projection: Projection
  events: RuntimeEvent[]
  cursors?: Record<string, number>
  executor?: Record<string, unknown>
  scheduler?: { mode?: string; running?: boolean; label?: string }
}

export type LiveChunk = {
  type?: string
  run_id: string
  root_run_id?: string
  invocation_id?: string | number
  segment?: number
  chunk?: number
  kind?: string
  text?: string
  finish_reason?: string
  tool_call_count?: number
  tool_name?: string
  tool_index?: number
  error?: { message?: string; code?: string; retryable?: boolean }
  [key: string]: unknown
}

export type LiveSegment = {
  key: string
  run_id: string
  invocation_id: string
  segment: number
  content: string
  reasoning: string
  phase: 'reasoning' | 'content' | 'tool' | 'done'
  updatedAt: number
}

export type TaskView = {
  id: string
  title: string
  status: 'pending' | 'running' | 'done' | 'blocked' | 'failed'
}
