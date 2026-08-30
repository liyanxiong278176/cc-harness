// 店铺资料: 编辑 + 营业状态
import { useEffect, useState } from 'react';
import { Card, Form, Input, InputNumber, Switch, Button, Space, Descriptions, message, Spin, Tag } from 'antd';
import { merchantAdminApi } from '../../api/merchantAdmin.js';
import { formatMoney } from '../../utils/format.js';

export default function MerchantProfilePage() {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [form] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      const p = await merchantAdminApi.myProfile();
      setProfile(p);
      form.setFieldsValue({
        name: p.name,
        description: p.description,
        category: p.category,
        campusZone: p.campusZone,
        deliveryFee: p.deliveryFee,
        minOrderAmount: p.minOrderAmount,
        openTime: p.openTime,
        closeTime: p.closeTime,
        logo: p.logo,
      });
    } catch (e) {
      message.error(e.message || '加载店铺信息失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async (values) => {
    setSaving(true);
    try {
      await merchantAdminApi.updateProfile(values);
      message.success('店铺资料已更新');
      load();
    } catch (e) {
      message.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const toggleOpen = async (checked) => {
    setToggling(true);
    try {
      await merchantAdminApi.setBusinessStatus(checked ? 1 : 0);
      message.success(checked ? '已开始营业' : '已停止营业');
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    } finally {
      setToggling(false);
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', paddingTop: 80 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!profile) return null;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card title="店铺信息">
        <Descriptions column={2} size="small">
          <Descriptions.Item label="评分">
            {Number(profile.rating || 0).toFixed(1)} ({profile.ratingCount || 0} 单)
          </Descriptions.Item>
          <Descriptions.Item label="营业状态">
            {profile.isOpen === 1 ? <Tag color="green">营业中</Tag> : <Tag color="default">已打烊</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="起送价">{formatMoney(profile.minOrderAmount)}</Descriptions.Item>
          <Descriptions.Item label="配送费">{formatMoney(profile.deliveryFee)}</Descriptions.Item>
        </Descriptions>
        <div style={{ marginTop: 8 }}>
          <Space>
            <span>营业状态开关:</span>
            <Switch checked={profile.isOpen === 1} loading={toggling} onChange={toggleOpen} />
          </Space>
        </div>
      </Card>

      <Card title="编辑资料">
        <Form form={form} layout="vertical" onFinish={save} style={{ maxWidth: 520 }}>
          <Form.Item name="name" label="店铺名称" rules={[{ required: true, message: '请输入店铺名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="logo" label="Logo URL">
            <Input placeholder="https://..." />
          </Form.Item>
          <Form.Item name="description" label="简介">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="category" label="分类" rules={[{ required: true, message: '请输入分类' }]}>
            <Input placeholder="如 快餐 / 奶茶" />
          </Form.Item>
          <Form.Item name="campusZone" label="校区" rules={[{ required: true, message: '请输入校区' }]}>
            <Input placeholder="如 主校区" />
          </Form.Item>
          <Form.Item name="deliveryFee" label="配送费(元)" rules={[{ required: true }]}>
            <InputNumber min={0} step={0.5} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="minOrderAmount" label="起送价(元)" rules={[{ required: true }]}>
            <InputNumber min={0} step={0.5} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="openTime" label="营业开始(如 09:00)">
            <Input />
          </Form.Item>
          <Form.Item name="closeTime" label="营业结束(如 22:00)">
            <Input />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={saving}>保存</Button>
          </Form.Item>
        </Form>
      </Card>
    </Space>
  );
}
