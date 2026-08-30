// 注册页(公开,注册角色固定 USER)
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Card, Form, Input, Button, message, Typography } from 'antd';
import { UserOutlined, LockOutlined, MobileOutlined, SmileOutlined } from '@ant-design/icons';
import { authApi } from '../api/auth.js';

export default function RegisterPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);

  const onFinish = async (values) => {
    setLoading(true);
    try {
      await authApi.register(values);
      message.success('注册成功,请登录');
      navigate('/login');
    } catch (e) {
      message.error(e.message || '注册失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 420, margin: '60px auto', padding: 16 }}>
      <Card title={<Typography.Title level={3} style={{ margin: 0 }}>注册新账号</Typography.Title>}>
        <Form onFinish={onFinish} layout="vertical">
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 4, max: 20, message: '用户名长度 4-20' },
            ]}
          >
            <Input prefix={<UserOutlined />} placeholder="4-20 位用户名" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, max: 32, message: '密码长度 6-32' },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="6-32 位密码" />
          </Form.Item>
          <Form.Item name="nickname" label="昵称(可选)">
            <Input prefix={<SmileOutlined />} placeholder="昵称" />
          </Form.Item>
          <Form.Item
            name="phone"
            label="手机号(可选)"
            rules={[{ pattern: /^$|^1\d{10}$/, message: '手机号格式不正确' }]}
          >
            <Input prefix={<MobileOutlined />} placeholder="1 开头的 11 位手机号" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>注册</Button>
          </Form.Item>
        </Form>
        <div style={{ textAlign: 'center' }}>
          已有账号? <Link to="/login">去登录</Link>
        </div>
      </Card>
    </div>
  );
}
