// 骑手域 API(docs/api.md §7):/api/rider/**
import { http } from './http.js';

export const riderApi = {
  /** 我的配送任务(query status,page,size) */
  tasks: (params) => http.get('/api/rider/tasks', { params }),
  /** 待接单池 */
  available: () => http.get('/api/rider/tasks/available'),
  /** 接单 */
  accept: (id) => http.post(`/api/rider/tasks/${id}/accept`),
  /** 取餐 */
  pickup: (id) => http.post(`/api/rider/tasks/${id}/pickup`),
  /** 送达 */
  deliver: (id) => http.post(`/api/rider/tasks/${id}/deliver`),
};
