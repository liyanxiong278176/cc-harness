// logic-harness: 库存扣减(防超卖)参考测试 —— 镜像 DishMapper.deductStock 条件更新语义
// 运行: node --test logic-harness/
import { test } from 'node:test';
import assert from 'node:assert/strict';

// 镜像 SQL: UPDATE dish SET stock=stock-qty, sold_count=sold_count+qty, version=version+1
//   WHERE id=? AND stock>=qty AND version=? AND deleted=0
function deductStock(dish, qty, expectVersion) {
  if (dish.deleted) return 0;
  if (dish.version !== expectVersion) return 0;
  if (dish.stock < qty) return 0;
  dish.stock -= qty;
  dish.soldCount += qty;
  dish.version += 1;
  return 1;
}

test('正常扣减: 库存充足且版本一致', () => {
  const d = { id: 1, stock: 10, soldCount: 2, version: 3, deleted: 0 };
  assert.equal(deductStock(d, 4, 3), 1);
  assert.equal(d.stock, 6);
  assert.equal(d.soldCount, 6);
  assert.equal(d.version, 4);
});

test('防超卖: 库存不足时拒绝扣减', () => {
  const d = { id: 2, stock: 3, soldCount: 0, version: 1, deleted: 0 };
  assert.equal(deductStock(d, 4, 1), 0);
  assert.equal(d.stock, 3); // 状态未被修改
});

test('乐观锁: 版本不一致(并发更新)时拒绝', () => {
  const d = { id: 3, stock: 10, soldCount: 0, version: 5, deleted: 0 };
  assert.equal(deductStock(d, 1, 4), 0);
  assert.equal(d.version, 5);
});

test('逻辑删除行不可扣减', () => {
  const d = { id: 4, stock: 10, soldCount: 0, version: 1, deleted: 1 };
  assert.equal(deductStock(d, 1, 1), 0);
});

test('边界: 库存恰好等于需求量', () => {
  const d = { id: 5, stock: 2, soldCount: 1, version: 1, deleted: 0 };
  assert.equal(deductStock(d, 2, 1), 1);
  assert.equal(d.stock, 0);
});

test('回补库存(退款/取消): 无条件加回', () => {
  const d = { id: 6, stock: 1, soldCount: 9, version: 2, deleted: 0 };
  d.stock += 3;
  d.soldCount -= 3;
  assert.equal(d.stock, 4);
  assert.equal(d.soldCount, 6);
});
