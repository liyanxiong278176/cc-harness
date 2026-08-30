// 路由守卫: 按角色拦截
import { Navigate, useLocation } from 'react-router-dom';
import { Spin } from 'antd';
import { useAuth } from '../store/AuthContext.jsx';

/**
 * 需登录的路由。allowRoles 为空数组表示任意登录角色。
 * 未登录 -> /login;角色不符 -> 对应角色首页。
 */
export function RequireAuth({ allowRoles = [], children }) {
  const { isLogin, user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }}>
        <Spin size="large" tip="加载中" />
      </div>
    );
  }
  if (!isLogin) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (allowRoles.length > 0 && !allowRoles.includes(user?.role)) {
    // 角色不符: 跳到自己的首页
    const home = user?.role === 'MERCHANT' ? '/merchant' : user?.role === 'RIDER' ? '/rider' : '/';
    return <Navigate to={home} replace />;
  }
  return children;
}
