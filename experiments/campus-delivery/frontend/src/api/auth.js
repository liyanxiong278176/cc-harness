// 认证域 API(docs/api.md §1):/api/auth/**
import { http } from './http.js';

export const authApi = {
  /** 注册(公开,角色固定 USER),返回 {token, user} */
  register: (payload) => http.post('/api/auth/register', payload),
  /** 登录(公开),返回 {token, user} */
  login: (payload) => http.post('/api/auth/login', payload),
  /** 当前登录用户信息 */
  me: () => http.get('/api/auth/me'),
  /** 修改密码 */
  changePassword: (payload) => http.put('/api/auth/password', payload),
};
