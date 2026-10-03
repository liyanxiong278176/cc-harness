import assert from 'node:assert/strict'
import test from 'node:test'
import { reduceStreamItem, type StreamItem, type StreamingState } from '../src/state/streaming.ts'

function apply(previous: StreamingState | undefined, item: StreamItem, now: number) {
  return reduceStreamItem(previous ? { [item.run_id ?? 'run-a']: previous } : {}, item, 'root-a', now)
}

function delta(segment: number, chunk: number, kind: StreamItem['kind'], text = ''): StreamItem {
  return { type: 'stream_delta', run_id: 'run-a', root_run_id: 'root-a', segment, chunk, kind, text }
}

test('思考增量与正文分开追加，正文开始后思考自动折叠并记录时长', () => {
  let current = apply(undefined, delta(0, 1, 'reasoning', '先检查输入。'), 1_000)['run-a']
  assert.equal(current.phase, 'reasoning')
  assert.equal(current.reasoning, '先检查输入。')
  assert.equal(current.text, '')
  assert.equal(current.reasoning_done, false)

  current = apply(current, delta(0, 2, 'content', '结果是 42。'), 2_400)['run-a']
  assert.equal(current.phase, 'content')
  assert.equal(current.reasoning, '先检查输入。')
  assert.equal(current.text, '结果是 42。')
  assert.equal(current.reasoning_done, true)
  assert.equal(current.reasoning_seconds, 1)

  current = apply(current, delta(0, 3, 'done'), 2_500)['run-a']
  assert.equal(current.phase, 'done')
  assert.equal(current.text, '结果是 42。')
})

test('重复 chunk 去重，缺口后冻结临时前缀', () => {
  const first = apply(undefined, delta(1, 4, 'content', 'prefix'), 1_000)['run-a']
  assert.equal(apply(first, delta(1, 4, 'content', 'duplicate'), 1_100)['run-a'], first)

  const gap = apply(first, {
    type: 'stream_gap', run_id: 'run-a', root_run_id: 'root-a', segment: 1, chunk: 5,
  }, 1_200)['run-a']
  assert.equal(gap.phase, 'gap')
  assert.equal(gap.frozen, true)
  assert.equal(apply(gap, delta(1, 6, 'content', 'late'), 1_300)['run-a'], gap)
})

test('模型流终态错误保留已生成前缀并显示失败态', () => {
  const partial = apply(undefined, delta(1, 1, 'content', '已生成内容'), 1_000)['run-a']
  const failed = apply(partial, {
    type: 'stream_error', run_id: 'run-a', root_run_id: 'root-a', segment: 1, chunk: 2,
    error: { message: '服务暂时不可用', retryable: true, next_action: '重试本轮' },
  }, 1_100)['run-a']

  assert.equal(failed.phase, 'failed')
  assert.equal(failed.text, '已生成内容')
  assert.equal(failed.frozen, true)
  assert.equal(failed.error?.retryable, true)
})

test('工具调用显示为工具阶段，下一段不拼接上一段的输出', () => {
  let current = apply(undefined, delta(2, 1, 'tool_call_delta'), 1_000)['run-a']
  current = reduceStreamItem(current ? { 'run-a': current } : {}, {
    ...delta(2, 2, 'done'), tool_call_count: 1,
  }, 'root-a', 1_100)['run-a']
  assert.equal(current.phase, 'tool')

  current = apply(current, delta(3, 1, 'content', 'next segment'), 2_000)['run-a']
  assert.equal(current.phase, 'content')
  assert.equal(current.text, 'next segment')
})
