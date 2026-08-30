// 统一请求封装(对接后端统一响应 Result<T>: {code, message, data})
// - 自动携带 JWT Bearer Token
// - code===0 返回 data;否则抛 BizError(code, message)
// - 401 时清空本地凭证并跳转登录页
// - 纯函数 buildUrl/parseResult 可脱离浏览器单测(见 tests/http.test.js)

const API_BASE = (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE)
  || '/api';

const TOKEN_KEY = 'campus_token';

export class BizError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'BizError';
    this.code = code;
  }
}

/** 拼装带 query 的 URL(避免 base 与 path 重复前缀,如 base=/api 且 path=/api/x)。 */
export function buildUrl(base, path, params) {
  const full = path && path.startsWith(base || '/') ? path : (base || '/') + path;
  const url = new URL(full, 'http://localhost');
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') {
        url.searchParams.set(k, String(v));
      }
    });
  }
  return url.pathname + url.search;
}

/** 解析后端统一响应;code===0 返回 data,否则抛 BizError。 */
export function parseResult(payload) {
  if (payload && payload.code === 0) {
    return payload.data;
  }
  throw new BizError(
    (payload && payload.code) || -1,
    (payload && payload.message) || '请求失败',
  );
}

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
}

export function setToken(token) {
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch (e) {
    /* 浏览器禁用 storage 时忽略 */
  }
}

/** 核心请求函数;http 注入点(测试可传入自定义 fetch)。 */
export async function request(path, { method = 'GET', params, body, headers = {}, fetchImpl } = {}) {
  const doFetch = fetchImpl || (typeof fetch === 'function' ? fetch : null);
  if (!doFetch) {
    throw new BizError(-2, '当前环境不支持网络请求');
  }
  const url = buildUrl(API_BASE, path, params);
  const token = getToken();
  const resp = await doFetch(url, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) {
    setToken('');
    if (typeof window !== 'undefined' && window.location) {
      window.location.href = '/login';
    }
    throw new BizError(40101, '未登录或登录已过期');
  }
  let payload;
  try {
    payload = await resp.json();
  } catch (e) {
    throw new BizError(resp.status, `非 JSON 响应(${resp.status})`);
  }
  return parseResult(payload);
}

export const http = {
  get: (path, opts) => request(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => request(path, { ...opts, method: 'POST', body }),
  put: (path, body, opts) => request(path, { ...opts, method: 'PUT', body }),
  delete: (path, opts) => request(path, { ...opts, method: 'DELETE' }),
};
