// 登录页(公开)
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Card, Form, Input, Button, message, Alert, Typography } from 'antd';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { authApi } from '../api/auth.js';
import { useAuth } from '../store/AuthContext.jsx';

export default function LoginPage() {
  const navigate = useNavigate();
  const { login } = useAuth();
  const [loading, setLoading] = useState(false);

  const homeByRole = (role) => {
    if (role === 'MERCHANT') return '/merchant';
    if (role === 'RIDER') return '/rider';
    return '/';
  };

  const onFinish = async (values) => {
    setLoading(true);
    try {
      const data = await authApi.login(values);
      login(data); // {token, user}
      message.success('登录成功');
      navigate(homeByRole(data.user?.role), { replace: true });
    } catch (e) {
      message.error(e.message || '登录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 420, margin: '60px auto', padding: 16 }}>
      <Card title={<Typography.Title level={3} style={{ margin: 0 }}>校园外卖登录</Typography.Title>}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="演示账号(密码均 123456): 用户 zhangsan / 商家 m_hanbao / 骑手 rider1"
        />
        <Form onFinish={onFinish} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>登录</Button>
          </Form.Item>
        </Form>
        <div style={{ textAlign: 'center' }}>
          还没有账号? <Link to="/register">去注册</Link>
        </div>
      </Card>
    </div>
  );
}
