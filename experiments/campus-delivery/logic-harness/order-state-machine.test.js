// logic-harness: 订单状态机参考测试 —— 镜像 OrderStateMachine 语义
// 运行: node --test logic-harness/
import { test } from 'node:test';
import assert from 'node:assert/strict';

const S = {
  CREATED: 'CREATED',
  PAID: 'PAID',
  PREPARING: 'PREPARING',
  DELIVERING: 'DELIVERING',
  COMPLETED: 'COMPLETED',
  CANCELLED: 'CANCELLED',
  REFUNDING: 'REFUNDING',
  REFUNDED: 'REFUNDED',
};

// 镜像 Java OrderStateMachine 的合法迁移表
const TRANSITIONS = new Map([
  [S.CREATED, new Set([S.PAID, S.CANCELLED])],
  [S.PAID, new Set([S.PREPARING, S.REFUNDING])],
  [S.PREPARING, new Set([S.DELIVERING, S.REFUNDING])],
  [S.DELIVERING, new Set([S.COMPLETED, S.REFUNDING])],
  [S.COMPLETED, new Set()],
  [S.CANCELLED, new Set()],
  [S.REFUNDING, new Set([S.REFUNDED, S.PAID])],
  [S.REFUNDED, new Set()],
]);

const canTransit = (from, to) => TRANSITIONS.get(from)?.has(to) ?? false;

test('正常链路: 创建->支付->备餐->配送->完成', () => {
  const path = [S.CREATED, S.PAID, S.PREPARING, S.DELIVERING, S.COMPLETED];
  for (let i = 0; i < path.length - 1; i++) {
    assert.ok(canTransit(path[i], path[i + 1]), `${path[i]} -> ${path[i + 1]}`);
  }
});

test('非法迁移被拒绝', () => {
  assert.equal(canTransit(S.CREATED, S.COMPLETED), false);
  assert.equal(canTransit(S.PAID, S.CANCELLED), false);
  assert.equal(canTransit(S.COMPLETED, S.REFUNDING), false);
  assert.equal(canTransit(S.CANCELLED, S.PAID), false);
});

test('退款路径: 支付后任意在途状态可申请退款', () => {
  for (const st of [S.PAID, S.PREPARING, S.DELIVERING]) {
    assert.ok(canTransit(st, S.REFUNDING), `${st} -> REFUNDING`);
  }
  // 退款中 -> 已退款,或商家拒绝退回 PAID
  assert.ok(canTransit(S.REFUNDING, S.REFUNDED));
  assert.ok(canTransit(S.REFUNDING, S.PAID));
});

test('终态不可再迁移', () => {
  for (const st of [S.COMPLETED, S.CANCELLED, S.REFUNDED]) {
    for (const to of Object.values(S)) {
      assert.equal(canTransit(st, to), false, `${st} -> ${to}`);
    }
  }
});
