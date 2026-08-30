// 商家浏览域 API(docs/api.md §3):/api/merchants/**
import { http } from './http.js';

export const merchantBrowseApi = {
  /** 商家列表: query zone,page,size;营业中优先 */
  page: (params) => http.get('/api/merchants', { params }),
  /** 商家详情 */
  detail: (id) => http.get(`/api/merchants/${id}`),
  /** 分类 + 上架菜品(Redis 缓存) */
  menu: (id) => http.get(`/api/merchants/${id}/menu`),
};
