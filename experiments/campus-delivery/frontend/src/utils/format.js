// 格式化工具(纯函数,无外部依赖,node:test 可测)
// 金额语义与后端 MoneyUtils 一致:BigDecimal 以元为单位,展示 2 位小数。

/** 金额展示: 元 -> "¥12.50";null/undefined -> "¥0.00" */
export function formatMoney(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '¥0.00';
  return `¥${n.toFixed(2)}`;
}

/** 金额展示但不带货币符号(用于拼凑文本) */
export function money(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '0.00';
  return n.toFixed(2);
}

/** 时间展示: ISO 字符串 -> "YYYY-MM-DD HH:mm" */
export function formatDateTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 订单状态 -> 中文(与后端 Constants.OrderStatus 一致) */
export const ORDER_STATUS_TEXT = {
  CREATED: '待支付',
  PAID: '已支付',
  PREPARING: '备餐中',
  DELIVERING: '配送中',
  COMPLETED: '已完成',
  CANCELLED: '已取消',
  REFUNDING: '退款中',
  REFUNDED: '已退款',
};

export function orderStatusText(status) {
  return ORDER_STATUS_TEXT[status] || status || '-';
}

/** 配送任务状态 -> 中文(与后端 Constants.DeliveryStatus 一致) */
export const DELIVERY_STATUS_TEXT = {
  WAIT_ACCEPT: '待接单',
  ACCEPTED: '已接单',
  PICKED: '已取餐',
  DELIVERING: '配送中',
  DELIVERED: '已送达',
  CANCELLED: '已取消',
};

export function deliveryStatusText(status) {
  return DELIVERY_STATUS_TEXT[status] || status || '-';
}

/** 优惠券类型 -> 中文(与后端 Constants.CouponType 一致) */
export const COUPON_TYPE_TEXT = {
  FULL_REDUCTION: '满减券',
  DISCOUNT: '折扣券',
};

export function couponTypeText(type) {
  return COUPON_TYPE_TEXT[type] || type || '-';
}

/** 用户券状态 -> 中文(与后端 Constants.UserCouponStatus 一致) */
export const COUPON_STATUS_TEXT = {
  UNUSED: '未使用',
  USED: '已使用',
  EXPIRED: '已过期',
};

export function couponStatusText(status) {
  return COUPON_STATUS_TEXT[status] || status || '-';
}

/** 通知类型 -> 中文(与后端 Constants.NotificationType 一致) */
export const NOTIFICATION_TYPE_TEXT = {
  ORDER_STATUS: '订单状态',
  PAYMENT: '支付',
  DELIVERY: '配送',
  SYSTEM: '系统',
};

export function notificationTypeText(type) {
  return NOTIFICATION_TYPE_TEXT[type] || type || '-';
}

/** 退款状态 -> 中文(与后端 Constants.RefundStatus 一致) */
export const REFUND_STATUS_TEXT = {
  PENDING: '待审核',
  APPROVED: '已同意',
  REJECTED: '已拒绝',
  REFUNDED: '已退款',
};

export function refundStatusText(status) {
  return REFUND_STATUS_TEXT[status] || status || '-';
}

/** 角色 -> 中文 */
export const ROLE_TEXT = {
  USER: '用户',
  MERCHANT: '商家',
  RIDER: '骑手',
  ADMIN: '管理员',
};

export function roleText(role) {
  return ROLE_TEXT[role] || role || '-';
}
