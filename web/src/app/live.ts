/** Ephemeral provider deltas. Durable events always replace the live prefix. */

import type { LiveChunk, LiveSegment } from './types'

function segmentKey(chunk: LiveChunk): string {
  return [
    chunk.run_id,
    chunk.invocation_id ?? 'inv',
    chunk.segment ?? 0,
  ].join(':')
}

export function applyLiveChunk(
  segments: LiveSegment[],
  chunk: LiveChunk,
  now = Date.now(),
): LiveSegment[] {
  if (!chunk || chunk.type === 'stream_gap') return segments
  if (chunk.kind === 'done') {
    const key = segmentKey(chunk)
    return segments.map(segment => segment.key === key
      ? { ...segment, phase: 'done', updatedAt: now }
      : segment)
  }
  if (!chunk.kind || !['content', 'reasoning', 'tool_call_delta'].includes(chunk.kind)) {
    return segments
  }
  const key = segmentKey(chunk)
  const index = segments.findIndex(segment => segment.key === key)
  const previous: LiveSegment = index >= 0
    ? segments[index]
    : {
      key,
      run_id: chunk.run_id,
      invocation_id: String(chunk.invocation_id ?? 'inv'),
      segment: Number(chunk.segment ?? 0),
      content: '',
      reasoning: '',
      phase: 'content',
      updatedAt: now,
    }
  const text = typeof chunk.text === 'string' ? chunk.text : ''
  let next: LiveSegment
  if (chunk.kind === 'reasoning') {
    next = { ...previous, reasoning: previous.reasoning + text, phase: 'reasoning', updatedAt: now }
  } else if (chunk.kind === 'content') {
    next = { ...previous, content: previous.content + text, phase: 'content', updatedAt: now }
  } else {
    next = { ...previous, phase: 'tool', updatedAt: now }
  }
  if (index < 0) return [...segments, next]
  const copy = segments.slice()
  copy[index] = next
  return copy
}

/** Drop live text for a run (or one segment of it) once durability catches up. */
export function clearLiveSegments(
  segments: LiveSegment[],
  runId: string,
  segment?: number,
): LiveSegment[] {
  return segments.filter(item => {
    if (item.run_id !== runId) return true
    if (segment === undefined) return false
    return item.segment !== segment
  })
}

/** Keep only recent ephemeral segments; a long session must not accumulate. */
export function pruneLiveSegments(segments: LiveSegment[], now = Date.now()): LiveSegment[] {
  // Keep a finished segment until its durable AssistantMessageCommitted
  // replaces it; dropping at `done` flashes the text away for one frame.
  const live = segments.filter(item => now - item.updatedAt < 120_000)
  return live.slice(-8)
}
