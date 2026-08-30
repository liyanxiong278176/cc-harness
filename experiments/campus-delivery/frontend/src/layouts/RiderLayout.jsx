// 骑手端布局: 侧边菜单 + 内容区
import { Outlet, useNavigate } from 'react-router-dom';
import { Layout, Menu, Button, Space, Tag } from 'antd';
import { DashboardOutlined, LogoutOutlined, CarryOutOutlined } from '@ant-design/icons';
import { useAuth } from '../store/AuthContext.jsx';

const { Sider, Header, Content } = Layout;

const RIDER_MENU = [
  { key: '/rider', icon: <CarryOutOutlined />, label: '我的任务' },
  { key: '/rider/available', icon: <DashboardOutlined />, label: '待接单池' },
];

export default function RiderLayout() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark">
        <div style={{ color: '#fff', padding: 16, fontSize: 16, fontWeight: 700 }}>骑手端</div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={RIDER_MENU}
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
            <Tag color="green">骑手端</Tag>
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
