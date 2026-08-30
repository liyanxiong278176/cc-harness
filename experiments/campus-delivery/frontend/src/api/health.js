// 系统健康检查 API(docs/api.md §0 健康检查 / docs/operations.md):GET /api/health
// 供运维/前端就绪探针使用:返回 {status, components:{db,redis,rabbit}, version, time}
import { http } from './http.js';

export const healthApi = {
  /** 获取后端各组件健康状态 */
  get: (opts) => http.get('/api/health', opts),
};
