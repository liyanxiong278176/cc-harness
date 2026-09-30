export type StreamItem = {
  type: 'stream_delta' | 'stream_gap' | 'stream_error'
  run_id?: string
  root_run_id?: string
  invocation_id?: string
  segment?: number
  chunk?: number
  kind?: 'content' | 'reasoning' | 'tool_call_delta' | 'done' | 'error'
  text?: string
  tool_name?: string | null
  finish_reason?: string | null
  tool_call_count?: number
  usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number } | null
  live_id?: number
  reason?: string
  error?: {
    message?: string
    next_action?: string
    code?: string
    retryable?: boolean
    partial_output?: boolean
    attempt?: number | null
    retry_after?: number | null
    request_id?: string
    phase?: string
  }
  ts?: number
}

export type StreamingState = {
  run_id: string
  root_run_id: string
  invocation_id?: string
  segment: number
  chunk: number
  text: string
  reasoning: string
  reasoning_started_at?: number
  reasoning_seconds?: number
  reasoning_done: boolean
  phase: 'content' | 'reasoning' | 'tool' | 'done' | 'gap' | 'stopped' | 'failed'
  tool_name?: string | null
  finish_reason?: string | null
  updated_at: number
  frozen?: boolean
  error?: StreamItem['error']
}

export function reduceStreamItem(
  current: Record<string, StreamingState>,
  next: StreamItem,
  streamSession: string,
  now: number,
): Record<string, StreamingState> {
  if (!next.run_id) return current
  const previous = current[next.run_id]
  const attemptChanged = Boolean(
    previous
    && next.invocation_id
    && previous.invocation_id !== next.invocation_id,
  )
  const previousAttempt = attemptChanged ? undefined : previous
  const segment = Number(next.segment ?? previousAttempt?.segment ?? 0)
  const chunk = Number(next.chunk ?? previousAttempt?.chunk ?? 0)
  if (next.type === 'stream_delta' && previousAttempt && segment === previousAttempt.segment && chunk > 0 && chunk <= previousAttempt.chunk) return current
  const sameSegment = previousAttempt?.segment === segment
  const base: StreamingState = previousAttempt ?? {
    run_id: next.run_id,
    root_run_id: next.root_run_id ?? streamSession,
    invocation_id: next.invocation_id,
    segment,
    chunk: 0,
    text: '',
    reasoning: '',
    reasoning_done: false,
    phase: 'content',
    updated_at: now,
  }
  // A provider boundary means the visible text is no longer a complete
  // prefix. Keep it frozen until a durable event reconciles the run.
  if (previousAttempt?.frozen && previousAttempt.phase === 'gap' && sameSegment && next.type === 'stream_delta') return current
  if (previousAttempt?.phase === 'done' && sameSegment && next.type === 'stream_delta' && next.kind !== 'done') return current
  if (next.type === 'stream_gap') {
    const gap: StreamingState = { ...base, segment, chunk, phase: 'gap', updated_at: now, frozen: true }
    return { ...current, [next.run_id]: gap }
  }
  if (next.type === 'stream_error' || next.kind === 'error') {
    const failed: StreamingState = {
      ...base,
      segment,
      chunk,
      phase: 'gap',
      updated_at: now,
      frozen: true,
      error: next.error,
    }
    return { ...current, [next.run_id]: failed }
  }

  let phase: StreamingState['phase'] = 'content'
  let reasoning = sameSegment ? base.reasoning : ''
  let reasoningStartedAt = sameSegment ? base.reasoning_started_at : undefined
  let reasoningSeconds = sameSegment ? base.reasoning_seconds : undefined
  let reasoningDone = sameSegment ? base.reasoning_done : false
  let text = sameSegment ? base.text : ''

  if (next.kind === 'reasoning') {
    phase = 'reasoning'
    reasoning += next.text ?? ''
    reasoningStartedAt ??= now
    reasoningDone = false
  } else if (next.kind === 'content') {
    text += next.text ?? ''
    if (reasoning && !reasoningDone) {
      reasoningDone = true
      reasoningSeconds = Math.max(1, Math.round((now - (reasoningStartedAt ?? now)) / 1000))
    }
  } else if (next.kind === 'tool_call_delta') {
    phase = 'tool'
    if (reasoning && !reasoningDone) {
      reasoningDone = true
      reasoningSeconds = Math.max(1, Math.round((now - (reasoningStartedAt ?? now)) / 1000))
    }
  } else if (next.kind === 'done') {
    phase = next.tool_call_count ? 'tool' : 'done'
    reasoningDone = true
    if (reasoning && reasoningSeconds === undefined) {
      reasoningSeconds = Math.max(1, Math.round((now - (reasoningStartedAt ?? now)) / 1000))
    }
  }

  const updated: StreamingState = {
    ...base,
    root_run_id: next.root_run_id ?? base.root_run_id,
    invocation_id: next.invocation_id ?? base.invocation_id,
    segment,
    chunk,
    phase,
    tool_name: sameSegment ? next.tool_name ?? base.tool_name : next.tool_name,
    finish_reason: sameSegment ? next.finish_reason ?? base.finish_reason : next.finish_reason,
    text,
    reasoning,
    reasoning_started_at: reasoningStartedAt,
    reasoning_seconds: reasoningSeconds,
    reasoning_done: reasoningDone,
    updated_at: now,
    frozen: false,
    error: sameSegment ? base.error : undefined,
  }
  return { ...current, [next.run_id]: updated }
}

export function reduceStreamBatch(
  current: Record<string, StreamingState>,
  items: StreamItem[],
  streamSession: string,
): Record<string, StreamingState> {
  let next = current
  for (const item of items) next = reduceStreamItem(next, item, streamSession, Date.now())
  return next
}
