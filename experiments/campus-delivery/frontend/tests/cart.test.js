// cart.js 购物车计算单测(离线 node:test)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  calcCheckedTotal, calcCheckedCount, calcAllCount, isCartEmpty, validateCheckout,
} from '../src/utils/cart.js';

const groups = [
  {
    merchantId: 1,
    items: [
      { dishId: 11, price: 10.5, quantity: 2, checked: 1 },
      { dishId: 12, price: 5, quantity: 1, checked: 0 },
    ],
  },
  {
    merchantId: 2,
    items: [
      { dishId: 21, price: 8, quantity: 3, checked: 1 },
    ],
  },
];

test('calcCheckedTotal: 仅统计勾选项', () => {
  assert.equal(calcCheckedTotal(groups), 10.5 * 2 + 8 * 3); // 21 + 24 = 45
});

test('calcCheckedCount: 勾选数量', () => {
  assert.equal(calcCheckedCount(groups), 2 + 3); // 5
});

test('calcAllCount: 全部数量', () => {
  assert.equal(calcAllCount(groups), 2 + 1 + 3); // 6
});

test('isCartEmpty', () => {
  assert.equal(isCartEmpty(groups), false);
  assert.equal(isCartEmpty([]), true);
  assert.equal(isCartEmpty([{ items: [] }]), true);
  assert.equal(isCartEmpty(null), true);
});

test('validateCheckout: 空车 / 未勾选 / 未达起送', () => {
  assert.equal(validateCheckout([]), '购物车为空');
  assert.equal(validateCheckout(null), '购物车为空');
  assert.equal(
    validateCheckout([{ items: [{ price: 1, quantity: 1, checked: 0 }] }]),
    '请勾选要结算的商品',
  );
  assert.equal(validateCheckout(groups, { minOrderAmount: 100 }), '未达起送金额 ¥100.00');
  assert.equal(validateCheckout(groups, { minOrderAmount: 10 }), null);
});
