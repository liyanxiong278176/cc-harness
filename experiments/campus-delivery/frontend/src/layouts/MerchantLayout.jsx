// 商家端布局: 侧边菜单 + 内容区
import { Outlet, useNavigate } from 'react-router-dom';
import { Layout, Menu, Button, Space, Tag } from 'antd';
import {
  DashboardOutlined, ProfileOutlined, AppstoreOutlined, ShoppingOutlined,
  CommentOutlined, RollbackOutlined, LogoutOutlined, ShopOutlined,
} from '@ant-design/icons';
import { useAuth } from '../store/AuthContext.jsx';

const { Sider, Header, Content } = Layout;

const MERCHANT_MENU = [
  { key: '/merchant', icon: <DashboardOutlined />, label: '工作台' },
  { key: '/merchant/profile', icon: <ProfileOutlined />, label: '店铺资料' },
  { key: '/merchant/categories', icon: <AppstoreOutlined />, label: '菜品分类' },
  { key: '/merchant/dishes', icon: <ShopOutlined />, label: '菜品管理' },
  { key: '/merchant/orders', icon: <ShoppingOutlined />, label: '订单管理' },
  { key: '/merchant/reviews', icon: <CommentOutlined />, label: '评价管理' },
  { key: '/merchant/refunds', icon: <RollbackOutlined />, label: '退款处理' },
];

export default function MerchantLayout() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark">
        <div style={{ color: '#fff', padding: 16, fontSize: 16, fontWeight: 700 }}>
          <ShopOutlined /> 商家管理
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={MERCHANT_MENU}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            paddingInline: 24,
          }}
        >
          <Space>
            <span style={{ fontWeight: 600 }}>{user?.nickname || user?.username}</span>
            <Tag color="orange">商家端</Tag>
          </Space>
          <Button
            type="text"
            icon={<LogoutOutlined />}
            onClick={() => {
              logout();
              navigate('/login');
            }}
          >
            退出登录
          </Button>
        </Header>
        <Content style={{ margin: 16 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
