import assert from 'node:assert/strict'
import test from 'node:test'
import { applyLiveChunk, clearLiveSegments, pruneLiveSegments } from '../src/app/live.ts'
import type { LiveChunk } from '../src/app/types.ts'

function chunk(kind: string, text = '', segment = 1): LiveChunk {
  return { type: 'stream_delta', run_id: 'run', invocation_id: 'inv', segment, kind, text }
}

test('content and reasoning accumulate on one segment', () => {
  let segments = applyLiveChunk([], chunk('reasoning', 'plan'), 1000)
  segments = applyLiveChunk(segments, chunk('content', 'answer'), 1100)
  assert.equal(segments[0]?.reasoning, 'plan')
  assert.equal(segments[0]?.content, 'answer')
  assert.equal(segments[0]?.phase, 'content')
})

test('done keeps the prefix until durability clears it', () => {
  let segments = applyLiveChunk([], chunk('content', 'answer'), 1000)
  segments = applyLiveChunk(segments, chunk('done'), 1200)
  assert.equal(segments[0]?.phase, 'done')
  assert.equal(segments[0]?.content, 'answer')
  segments = clearLiveSegments(segments, 'run')
  assert.equal(segments.length, 0)
})

test('clear can target one run and prune bounds history', () => {
  let segments = applyLiveChunk([], chunk('content', 'a', 1), 1000)
  segments = applyLiveChunk(segments, { ...chunk('content', 'b', 2), run_id: 'other' }, 1000)
  assert.equal(clearLiveSegments(segments, 'run').length, 1)
  assert.equal(pruneLiveSegments(segments, 3_000_000).length, 0)
})
