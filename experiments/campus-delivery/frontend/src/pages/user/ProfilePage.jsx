// 个人中心: 资料编辑 + 修改密码
import { useState } from 'react';
import { Card, Descriptions, Form, Input, Button, message, Tabs, Space, Avatar, Tag } from 'antd';
import { UserOutlined } from '@ant-design/icons';
import { useAuth } from '../../store/AuthContext.jsx';
import { userApi } from '../../api/user.js';
import { authApi } from '../../api/auth.js';
import { roleText } from '../../utils/format.js';

function maskPhone(phone) {
  if (!phone || phone.length < 7) return phone || '-';
  return `${phone.slice(0, 3)}****${phone.slice(-4)}`;
}

export default function ProfilePage() {
  const { user, refresh } = useAuth();
  const [saving, setSaving] = useState(false);
  const [pwdSaving, setPwdSaving] = useState(false);

  const saveProfile = async (values) => {
    setSaving(true);
    try {
      await userApi.updateProfile(values);
      message.success('资料已更新');
      await refresh();
    } catch (e) {
      message.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const changePwd = async (values) => {
    setPwdSaving(true);
    try {
      await authApi.changePassword({ oldPassword: values.oldPassword, newPassword: values.newPassword });
      message.success('密码已修改');
    } catch (e) {
      message.error(e.message || '修改失败');
    } finally {
      setPwdSaving(false);
    }
  };

  return (
    <Card title="个人中心">
      <Tabs
        items={[
          {
            key: 'info',
            label: '基本信息',
            children: (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <Descriptions column={2} bordered size="small">
                  <Descriptions.Item label="用户名">{user?.username}</Descriptions.Item>
                  <Descriptions.Item label="角色"><Tag color="blue">{roleText(user?.role)}</Tag></Descriptions.Item>
                  <Descriptions.Item label="昵称">{user?.nickname || '-'}</Descriptions.Item>
                  <Descriptions.Item label="手机号">{maskPhone(user?.phone)}</Descriptions.Item>
                </Descriptions>
                <Form
                  layout="inline"
                  initialValues={{ nickname: user?.nickname || '', avatar: user?.avatar || '', phone: user?.phone || '' }}
                  onFinish={saveProfile}
                >
                  <Form.Item name="nickname" label="昵称" rules={[{ max: 32 }]}>
                    <Input style={{ width: 160 }} placeholder="昵称" />
                  </Form.Item>
                  <Form.Item name="phone" label="手机号" rules={[{ pattern: /^$|^1\d{10}$/, message: '格式不正确' }]}>
                    <Input style={{ width: 160 }} placeholder="手机号" />
                  </Form.Item>
                  <Form.Item name="avatar" label="头像URL" rules={[{ type: 'url', message: '需为URL' }]}>
                    <Input style={{ width: 220 }} placeholder="头像链接" />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" loading={saving}>保存</Button>
                  </Form.Item>
                </Form>
              </Space>
            ),
          },
          {
            key: 'pwd',
            label: '修改密码',
            children: (
              <Form layout="vertical" style={{ maxWidth: 360 }} onFinish={changePwd}>
                <Form.Item name="oldPassword" label="原密码" rules={[{ required: true, message: '请输入原密码' }]}>
                  <Input.Password placeholder="原密码" />
                </Form.Item>
                <Form.Item
                  name="newPassword"
                  label="新密码"
                  rules={[
                    { required: true, message: '请输入新密码' },
                    { min: 6, max: 32, message: '密码长度 6-32' },
                  ]}
                >
                  <Input.Password placeholder="6-32 位新密码" />
                </Form.Item>
                <Form.Item
                  name="confirm"
                  label="确认新密码"
                  dependencies={['newPassword']}
                  rules={[
                    { required: true, message: '请再次输入新密码' },
                    ({ getFieldValue }) => ({
                      validator(_, value) {
                        if (!value || getFieldValue('newPassword') === value) return Promise.resolve();
                        return Promise.reject(new Error('两次密码不一致'));
                      },
                    }),
                  ]}
                >
                  <Input.Password placeholder="确认新密码" />
                </Form.Item>
                <Form.Item>
                  <Button type="primary" htmlType="submit" loading={pwdSaving}>修改密码</Button>
                </Form.Item>
              </Form>
            ),
          },
        ]}
      />
    </Card>
  );
}
