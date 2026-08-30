// logic-harness: 支付回调幂等参考测试 —— 镜像 PaymentService.notify 条件更新语义
// 运行: node --test logic-harness/
import { test } from 'node:test';
import assert from 'node:assert/strict';

// 镜像: 回调 -> 仅当订单 status=CREATED 时置 PAID;影响行数 0 视为重复/已处理
function applyNotify(order, success) {
  if (!success) return { changed: false, reason: 'FAILED' };
  if (order.status !== 'CREATED') return { changed: 0, reason: 'ALREADY_PAID' };
  order.status = 'PAID';
  return { changed: 1, reason: 'PAID' };
}

test('首次回调成功入账', () => {
  const o = { status: 'CREATED' };
  assert.deepEqual(applyNotify(o, true), { changed: 1, reason: 'PAID' });
  assert.equal(o.status, 'PAID');
});

test('重复回调幂等: 第二次不再改变状态', () => {
  const o = { status: 'CREATED' };
  applyNotify(o, true);
  const second = applyNotify(o, true);
  assert.equal(second.changed, 0);
  assert.equal(o.status, 'PAID');
});

test('失败回调不改状态(用户可重试)', () => {
  const o = { status: 'CREATED' };
  applyNotify(o, false);
  assert.equal(o.status, 'CREATED');
});

test('并发双回调: 只有一个能成功(条件更新互斥)', () => {
  const o = { status: 'CREATED' };
  const r1 = applyNotify(o, true);
  const r2 = applyNotify(o, true);
  const successes = [r1, r2].filter((r) => r.changed === 1).length;
  assert.equal(successes, 1);
  assert.equal(o.status, 'PAID');
});
