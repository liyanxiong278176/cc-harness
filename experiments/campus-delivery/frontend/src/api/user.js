// 用户域 API(docs/api.md §2):/api/user/**
import { http } from './http.js';

export const userApi = {
  /** 更新个人信息 */
  updateProfile: (payload) => http.put('/api/user/profile', payload),
  /** 地址列表 */
  listAddresses: () => http.get('/api/user/addresses'),
  /** 新增地址 */
  addAddress: (payload) => http.post('/api/user/addresses', payload),
  /** 修改地址 */
  updateAddress: (id, payload) => http.put(`/api/user/addresses/${id}`, payload),
  /** 删除地址 */
  deleteAddress: (id) => http.delete(`/api/user/addresses/${id}`),
  /** 我的优惠券(query status) */
  myCoupons: (params) => http.get('/api/user/coupons', { params }),
  /** 领取优惠券 */
  receiveCoupon: (couponId) => http.post(`/api/user/coupons/${couponId}/receive`),
  /** 通知分页 */
  notifications: (params) => http.get('/api/user/notifications', { params }),
  /** 标记已读 */
  markRead: (id) => http.put(`/api/user/notifications/${id}/read`),
  /** 全部已读 */
  markAllRead: () => http.put('/api/user/notifications/read-all'),
  /** 未读数 */
  unreadCount: () => http.get('/api/user/notifications/unread-count'),
};
