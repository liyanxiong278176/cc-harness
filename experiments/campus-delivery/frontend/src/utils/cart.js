// 购物车计算工具(纯函数,镜像后端 CartPriceCalculator 语义;node:test 可测)

/** 计算勾选商品的总金额(元,保留 2 位) */
export function calcCheckedTotal(groups) {
  let total = 0;
  (groups || []).forEach((g) => {
    (g.items || []).forEach((it) => {
      if (it.checked) {
        total += Number(it.price || 0) * Number(it.quantity || 0);
      }
    });
  });
  return round2(total);
}

/** 计算勾选商品数量 */
export function calcCheckedCount(groups) {
  let count = 0;
  (groups || []).forEach((g) => {
    (g.items || []).forEach((it) => {
      if (it.checked) count += Number(it.quantity || 0);
    });
  });
  return count;
}

/** 全部商品数量(含未勾选) */
export function calcAllCount(groups) {
  let count = 0;
  (groups || []).forEach((g) => {
    (g.items || []).forEach((it) => {
      count += Number(it.quantity || 0);
    });
  });
  return count;
}

/** 购物车是否为空 */
export function isCartEmpty(groups) {
  return !(groups || []).some((g) => (g.items || []).length > 0);
}

/** 下单前置校验: 返回错误信息,无错误返回 null */
export function validateCheckout(groups, { minOrderAmount = 0, deliveryFee = 0 } = {}) {
  if (isCartEmpty(groups)) return '购物车为空';
  const total = calcCheckedTotal(groups);
  if (total <= 0) return '请勾选要结算的商品';
  if (total < Number(minOrderAmount || 0)) return `未达起送金额 ¥${money2(minOrderAmount)}`;
  return null;
}

function round2(n) {
  return Math.round((Number(n) + Number.EPSILON) * 100) / 100;
}

function money2(n) {
  return Number(n || 0).toFixed(2);
}
