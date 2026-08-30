// 收货地址管理: 增删改
import { useCallback, useEffect, useState } from 'react';
import {
  Card, Table, Button, Modal, Form, Input, Switch, Popconfirm, Tag, message, Empty,
} from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { userApi } from '../../api/user.js';

export default function AddressesPage() {
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await userApi.listAddresses();
      setList(res || []);
    } catch (e) {
      message.error(e.message || '加载地址失败');
      setList([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openAdd = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ isDefault: false });
    setModalOpen(true);
  };

  const openEdit = (row) => {
    setEditing(row);
    form.setFieldsValue({
      receiverName: row.receiverName,
      receiverPhone: row.receiverPhone,
      campusZone: row.campusZone,
      detail: row.detail,
      isDefault: row.isDefault === 1,
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    const payload = { ...values, isDefault: values.isDefault ? 1 : 0 };
    setSaving(true);
    try {
      if (editing) {
        await userApi.updateAddress(editing.id, payload);
      } else {
        await userApi.addAddress(payload);
      }
      message.success('保存成功');
      setModalOpen(false);
      load();
    } catch (e) {
      message.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id) => {
    try {
      await userApi.deleteAddress(id);
      message.success('已删除');
      load();
    } catch (e) {
      message.error(e.message || '删除失败');
    }
  };

  return (
    <Card
      title="收货地址"
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={openAdd}>新增地址</Button>}
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={list}
        locale={{ emptyText: <Empty description="暂无地址" /> }}
        pagination={false}
        columns={[
          { title: '收货人', dataIndex: 'receiverName', width: 120 },
          { title: '手机号', dataIndex: 'receiverPhone', width: 140 },
          { title: '校区', dataIndex: 'campusZone', width: 120 },
          { title: '详细地址', dataIndex: 'detail' },
          {
            title: '默认',
            dataIndex: 'isDefault',
            width: 90,
            render: (v) => (v === 1 ? <Tag color="blue">默认</Tag> : '-'),
          },
          {
            title: '操作',
            key: 'op',
            width: 140,
            render: (_, r) => (
              <>
                <Button type="link" size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="确认删除该地址?" onConfirm={() => remove(r.id)}>
                  <Button type="link" size="small" danger>删除</Button>
                </Popconfirm>
              </>
            ),
          },
        ]}
      />

      <Modal
        title={editing ? '编辑地址' : '新增地址'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={submit}
        confirmLoading={saving}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="receiverName" label="收货人" rules={[{ required: true, message: '请输入收货人' }]}>
            <Input placeholder="收货人姓名" />
          </Form.Item>
          <Form.Item
            name="receiverPhone"
            label="手机号"
            rules={[{ required: true, pattern: /^1\d{10}$/, message: '请输入正确手机号' }]}
          >
            <Input placeholder="1 开头的 11 位手机号" />
          </Form.Item>
          <Form.Item name="campusZone" label="校区" rules={[{ required: true, message: '请选择校区' }]}>
            <Input placeholder="如 主校区 / 东校区" />
          </Form.Item>
          <Form.Item name="detail" label="详细地址" rules={[{ required: true, message: '请输入详细地址' }]}>
            <Input placeholder="楼栋-宿舍/教室" />
          </Form.Item>
          <Form.Item name="isDefault" label="设为默认地址" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
