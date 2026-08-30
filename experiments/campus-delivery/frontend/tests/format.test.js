// format.js 纯函数单测(离线 node:test,无第三方依赖)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  formatMoney, money, formatDateTime, orderStatusText, deliveryStatusText,
  couponTypeText, couponStatusText, notificationTypeText, refundStatusText, roleText,
} from '../src/utils/format.js';

test('formatMoney: 元 -> ¥x.xx', () => {
  assert.equal(formatMoney(12.5), '¥12.50');
  assert.equal(formatMoney(0), '¥0.00');
  assert.equal(formatMoney(null), '¥0.00');
  assert.equal(formatMoney(undefined), '¥0.00');
  assert.equal(formatMoney(9.999), '¥10.00');
  assert.equal(formatMoney('3.1'), '¥3.10');
});

test('money: 无符号金额 2 位小数', () => {
  assert.equal(money(5), '5.00');
  assert.equal(money(12.345), '12.35');
  assert.equal(money('7.5'), '7.50');
});

test('formatDateTime: ISO -> YYYY-MM-DD HH:mm', () => {
  assert.equal(formatDateTime('2025-01-02T03:04:05'), '2025-01-02 03:04');
  assert.equal(formatDateTime(null), '-');
  assert.equal(formatDateTime(''), '-');
  assert.equal(formatDateTime('not-a-date'), 'not-a-date');
});

test('状态文案映射与后端常量一致', () => {
  assert.equal(orderStatusText('CREATED'), '待支付');
  assert.equal(orderStatusText('COMPLETED'), '已完成');
  assert.equal(orderStatusText('CANCELLED'), '已取消');
  assert.equal(orderStatusText('UNKNOWN'), 'UNKNOWN');
  assert.equal(deliveryStatusText('WAIT_ACCEPT'), '待接单');
  assert.equal(deliveryStatusText('DELIVERED'), '已送达');
  assert.equal(couponTypeText('FULL_REDUCTION'), '满减券');
  assert.equal(couponTypeText('DISCOUNT'), '折扣券');
  assert.equal(couponStatusText('UNUSED'), '未使用');
  assert.equal(notificationTypeText('ORDER_STATUS'), '订单状态');
  assert.equal(refundStatusText('PENDING'), '待审核');
  assert.equal(roleText('MERCHANT'), '商家');
  assert.equal(roleText('RIDER'), '骑手');
  assert.equal(roleText('ADMIN'), '管理员');
});
