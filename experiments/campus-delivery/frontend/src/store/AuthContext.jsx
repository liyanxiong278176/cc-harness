// 全局登录态: AuthContext
// - token 存 localStorage(campus_token),user 存 localStorage(campus_user)
// - 提供 login/logout/refresh;按角色提供便捷判断
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { authApi } from '../api/auth.js';
import { getToken, setToken } from '../api/http.js';

const AuthContext = createContext(null);
const USER_KEY = 'campus_user';

function readStoredUser() {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(readStoredUser);
  const [loading, setLoading] = useState(false);

  const persistUser = useCallback((u) => {
    setUser(u);
    try {
      if (u) {
        localStorage.setItem(USER_KEY, JSON.stringify(u));
      } else {
        localStorage.removeItem(USER_KEY);
      }
    } catch (e) {
      /* ignore */
    }
  }, []);

  /** 登录成功后写入 token + user */
  const login = useCallback(({ token, user: u }) => {
    setToken(token);
    persistUser(u);
  }, [persistUser]);

  const logout = useCallback(() => {
    setToken('');
    persistUser(null);
  }, [persistUser]);

  /** 刷新当前用户信息(如资料变更后) */
  const refresh = useCallback(async () => {
    if (!getToken()) return null;
    const u = await authApi.me();
    persistUser(u);
    return u;
  }, [persistUser]);

  useEffect(() => {
    if (getToken() && !user) {
      setLoading(true);
      authApi.me()
        .then((u) => persistUser(u))
        .catch(() => setToken(''))
        .finally(() => setLoading(false));
    }
  }, [user, persistUser]);

  const value = useMemo(() => ({
    user,
    token: getToken(),
    loading,
    isLogin: !!user && !!getToken(),
    isUser: user?.role === 'USER',
    isMerchant: user?.role === 'MERCHANT',
    isRider: user?.role === 'RIDER',
    isAdmin: user?.role === 'ADMIN',
    login,
    logout,
    refresh,
  }), [user, loading, login, logout, refresh]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内使用');
  return ctx;
}
