// 购物车域 API(docs/api.md §4):/api/cart/**
import { http } from './http.js';

export const cartApi = {
  /** 购物车(按店铺分组) */
  get: () => http.get('/api/cart'),
  /** 加购 */
  addItem: (payload) => http.post('/api/cart/items', payload),
  /** 改数量 */
  updateQuantity: (dishId, quantity) => http.put(`/api/cart/items/${dishId}`, { quantity }),
  /** 勾选/取消勾选 */
  updateChecked: (dishId, checked) => http.put(`/api/cart/items/${dishId}/check`, { checked }),
  /** 移除单项 */
  removeItem: (dishId) => http.delete(`/api/cart/items/${dishId}`),
  /** 清空已勾选 */
  clearChecked: () => http.delete('/api/cart'),
};
