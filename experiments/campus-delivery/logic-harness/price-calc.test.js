// logic-harness: 订单价格计算参考测试 —— 镜像 OrderService.checkout / CartPriceCalculator 语义
// 运行: node --test logic-harness/
import { test } from 'node:test';
import assert from 'node:assert/strict';

// 金额以「分」为整数运算,避免浮点误差(与 Java MoneyUtils 一致)
const cents = (yuan) => Math.round(yuan * 100);

function calcPrice(items, deliveryFeeCents, discountCents) {
  const goodsCents = items.reduce((s, i) => s + i.priceCents * i.qty, 0);
  const payCents = goodsCents + deliveryFeeCents - discountCents;
  return { goodsCents, deliveryFeeCents, discountCents, payCents };
}

test('合计 = 商品 + 配送费 - 优惠', () => {
  const r = calcPrice(
    [{ priceCents: cents(12.5), qty: 2 }, { priceCents: cents(3), qty: 1 }],
    cents(2),
    cents(5),
  );
  assert.equal(r.goodsCents, 2800); // 25 + 3
  assert.equal(r.payCents, 2500); // 28 + 2 - 5
});

test('免配送费门槛: 商品满 20 元免配送费', () => {
  const fee = (goodsCents) => (goodsCents >= cents(20) ? 0 : cents(2));
  assert.equal(fee(cents(19.99)), 200);
  assert.equal(fee(cents(20)), 0);
});

test('优惠金额不超过应付(极端大额优惠被截断)', () => {
  const r = calcPrice([{ priceCents: cents(5), qty: 1 }], 0, cents(99));
  assert.ok(r.payCents <= r.goodsCents);
});

test('整数分运算无浮点误差', () => {
  const r = calcPrice([{ priceCents: cents(0.1), qty: 3 }], cents(0.2), 0);
  assert.equal(r.goodsCents, 30); // 0.1*3 精确为 30 分
  assert.equal(r.payCents, 50);
});
