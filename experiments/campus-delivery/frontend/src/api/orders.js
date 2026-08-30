// 订单域 API(docs/api.md §5):/api/orders/**
import { http } from './http.js';

export const orderApi = {
  /** 结算下单,返回 orderNo */
  checkout: (payload) => http.post('/api/orders/checkout', payload),
  /** 我的订单(query status,page,size) */
  page: (params) => http.get('/api/orders', { params }),
  /** 订单详情 */
  detail: (orderNo) => http.get(`/api/orders/${orderNo}`),
  /** 取消订单 */
  cancel: (orderNo, reason) => http.post(`/api/orders/${orderNo}/cancel`, { reason }),
  /** 发起支付,返回 {payUrl, orderNo, ...} */
  pay: (orderNo, channel) => http.post(`/api/orders/${orderNo}/pay`, { channel }),
  /** 申请退款 */
  refund: (orderNo, reason) => http.post(`/api/orders/${orderNo}/refund`, { reason }),
  /** 评价订单 */
  review: (orderNo, payload) => http.post(`/api/orders/${orderNo}/review`, payload),
  /** 订单配送跟踪 */
  track: (orderNo) => http.get(`/api/orders/${orderNo}/track`),
};
