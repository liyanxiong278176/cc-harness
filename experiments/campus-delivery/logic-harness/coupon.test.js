// logic-harness: 优惠券计算参考测试 —— 镜像 CouponCalculator 语义
// 运行: node --test logic-harness/
import { test } from 'node:test';
import assert from 'node:assert/strict';

const FULL_REDUCTION = 'FULL_REDUCTION';
const DISCOUNT = 'DISCOUNT';

// 金额以分计;镜像 CouponCalculator.discountAmount
function discountAmount(coupon, goodsCents, now, nowIso) {
  if (!coupon) return 0;
  if (nowIso < coupon.startTime || nowIso > coupon.endTime) return 0;
  if (goodsCents < coupon.thresholdCents) return 0;
  if (coupon.type === FULL_REDUCTION) return coupon.discountCents;
  if (coupon.type === DISCOUNT) {
    // 优惠 = 商品额 * (1 - 折扣率);rate=0.900 表示 9 折
    return Math.round(goodsCents * (1 - coupon.rate));
  }
  return 0;
}

test('满减: 达到门槛全额优惠', () => {
  const c = { type: FULL_REDUCTION, thresholdCents: 3000, discountCents: 500, startTime: '2024-01-01', endTime: '2030-01-01' };
  assert.equal(discountAmount(c, 3000, new Date(), '2025-01-01'), 500);
});

test('满减: 未达门槛不优惠', () => {
  const c = { type: FULL_REDUCTION, thresholdCents: 3000, discountCents: 500, startTime: '2024-01-01', endTime: '2030-01-01' };
  assert.equal(discountAmount(c, 2999, new Date(), '2025-01-01'), 0);
});

test('折扣券: 9 折 = 省 10%', () => {
  const c = { type: DISCOUNT, thresholdCents: 1000, rate: 0.9, startTime: '2024-01-01', endTime: '2030-01-01' };
  assert.equal(discountAmount(c, 10000, new Date(), '2025-01-01'), 1000);
});

test('过期券不生效', () => {
  const c = { type: FULL_REDUCTION, thresholdCents: 0, discountCents: 1000, startTime: '2024-01-01', endTime: '2024-12-31' };
  assert.equal(discountAmount(c, 999999, new Date(), '2025-06-01'), 0);
});

test('无券/空券返回 0', () => {
  assert.equal(discountAmount(null, 10000, new Date(), '2025-01-01'), 0);
});

test('折扣券优惠金额取整到分', () => {
  const c = { type: DISCOUNT, thresholdCents: 0, rate: 0.85, startTime: '2024-01-01', endTime: '2030-01-01' };
  // 10001 * 0.15 = 1500.15 -> 1500
  assert.equal(discountAmount(c, 10001, new Date(), '2025-01-01'), 1500);
});
