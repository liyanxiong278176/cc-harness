// 用户端布局: 顶部导航 + 内容区
import { Outlet, Link, useNavigate } from 'react-router-dom';
import { Layout, Menu, Badge, Dropdown, Button, Space } from 'antd';
import {
  HomeOutlined, ShoppingCartOutlined, UserOutlined, BellOutlined, LogoutOutlined,
  ProfileOutlined, TagsOutlined, EnvironmentOutlined, CarryOutOutlined,
} from '@ant-design/icons';
import { useEffect, useState } from 'react';
import { useAuth } from '../store/AuthContext.jsx';
import { userApi } from '../api/user.js';

const { Header, Content } = Layout;

const USER_MENU = [
  { key: '/', icon: <HomeOutlined />, label: '点餐' },
  { key: '/cart', icon: <ShoppingCartOutlined />, label: '购物车' },
  { key: '/orders', icon: <CarryOutOutlined />, label: '我的订单' },
  { key: '/coupons', icon: <TagsOutlined />, label: '优惠券' },
  { key: '/addresses', icon: <EnvironmentOutlined />, label: '收货地址' },
  { key: '/profile', icon: <ProfileOutlined />, label: '个人中心' },
];

export default function UserLayout() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    userApi.unreadCount().then(setUnread).catch(() => {});
    const t = setInterval(() => {
      userApi.unreadCount().then(setUnread).catch(() => {});
    }, 30000);
    return () => clearInterval(t);
  }, []);

  const dropdownItems = [
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
  ];

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <div style={{ color: '#fff', fontSize: 18, fontWeight: 700 }}>校园外卖</div>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[location.pathname]}
          items={USER_MENU}
          onClick={({ key }) => navigate(key)}
          style={{ flex: 1, minWidth: 0 }}
        />
        <Space size="middle">
          <Badge count={unread} size="small">
            <Button
              type="text"
              icon={<BellOutlined style={{ color: '#fff', fontSize: 18 }} />}
              onClick={() => navigate('/notifications')}
            />
          </Badge>
          <Dropdown
            menu={{
              items: dropdownItems,
              onClick: ({ key }) => {
                if (key === 'logout') {
                  logout();
                  navigate('/login');
                }
              },
            }}
          >
            <Button type="text" icon={<UserOutlined style={{ color: '#fff' }} />}>
              <span style={{ color: '#fff' }}>{user?.nickname || user?.username}</span>
            </Button>
          </Dropdown>
        </Space>
      </Header>
      <Content>
        <div className="page-container">
          <Outlet />
        </div>
      </Content>
    </Layout>
  );
}
