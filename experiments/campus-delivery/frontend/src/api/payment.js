// 模拟支付 API(docs/api.md §6):/api/payment/**
import { http } from './http.js';

export const paymentApi = {
  /** 模拟渠道回调(幂等) */
  mockNotify: (payload) => http.post('/api/payment/mock/notify', payload),
  /** 手动测试入口: query orderNo,success,channel */
  mockNotifyGet: (params) => http.get('/api/payment/mock/notify', { params }),
};
