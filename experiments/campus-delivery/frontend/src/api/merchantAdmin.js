// 商家管理端 API(docs/api.md §8):/api/merchant/**(MERCHANT 角色)
import { http } from './http.js';

export const merchantAdminApi = {
  /** 工作台统计 */
  dashboard: () => http.get('/api/merchant/dashboard'),
  /** 我的店铺资料 */
  myProfile: () => http.get('/api/merchant/profile'),
  /** 更新店铺资料 */
  updateProfile: (payload) => http.put('/api/merchant/profile', payload),
  /** 营业状态开关 */
  setBusinessStatus: (isOpen) => http.put('/api/merchant/business-status', { isOpen }),

  /** 分类列表 */
  categories: () => http.get('/api/merchant/categories'),
  /** 新增分类 */
  addCategory: (payload) => http.post('/api/merchant/categories', payload),
  /** 修改分类 */
  updateCategory: (id, payload) => http.put(`/api/merchant/categories/${id}`, payload),
  /** 删除分类 */
  deleteCategory: (id) => http.delete(`/api/merchant/categories/${id}`),

  /** 菜品分页(query categoryId,status,page,size) */
  dishes: (params) => http.get('/api/merchant/dishes', { params }),
  /** 新增菜品 */
  addDish: (payload) => http.post('/api/merchant/dishes', payload),
  /** 修改菜品 */
  updateDish: (id, payload) => http.put(`/api/merchant/dishes/${id}`, payload),
  /** 修改库存 */
  setStock: (id, stock) => http.put(`/api/merchant/dishes/${id}/stock`, { stock }),
  /** 上架/下架 */
  setStatus: (id, status) => http.put(`/api/merchant/dishes/${id}/status`, { status }),

  /** 店铺订单(query status,page,size) */
  orders: (params) => http.get('/api/merchant/orders', { params }),
  /** 接单 */
  acceptOrder: (orderNo) => http.post(`/api/merchant/orders/${orderNo}/accept`),
  /** 出餐完成 */
  readyOrder: (orderNo) => http.post(`/api/merchant/orders/${orderNo}/ready`),

  /** 评价列表 */
  reviews: (params) => http.get('/api/merchant/reviews', { params }),
  /** 回复评价 */
  replyReview: (id, reply) => http.post(`/api/merchant/reviews/${id}/reply`, { reply }),

  /** 退款申请列表 */
  refunds: (params) => http.get('/api/merchant/refunds', { params }),
  /** 同意退款 */
  approveRefund: (id) => http.post(`/api/merchant/refunds/${id}/approve`),
  /** 拒绝退款 */
  rejectRefund: (id, reason) => http.post(`/api/merchant/refunds/${id}/reject`, { reason }),
};
