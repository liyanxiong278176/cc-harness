// 菜品分类管理
import { useEffect, useState } from 'react';
import { Card, Table, Button, Modal, Form, Input, InputNumber, Popconfirm, Tag, message, Empty } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { merchantAdminApi } from '../../api/merchantAdmin.js';

export default function CategoriesPage() {
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      const res = await merchantAdminApi.categories();
      setList(res || []);
    } catch (e) {
      message.error(e.message || '加载分类失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const openAdd = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ sortOrder: 0 });
    setModalOpen(true);
  };

  const openEdit = (row) => {
    setEditing(row);
    form.setFieldsValue({ name: row.name, sortOrder: row.sortOrder || 0 });
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        await merchantAdminApi.updateCategory(editing.id, values);
      } else {
        await merchantAdminApi.addCategory(values);
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
      await merchantAdminApi.deleteCategory(id);
      message.success('已删除');
      load();
    } catch (e) {
      message.error(e.message || '删除失败(分类下可能还有菜品)');
    }
  };

  return (
    <Card
      title="菜品分类"
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={openAdd}>新增分类</Button>}
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={list}
        locale={{ emptyText: <Empty description="暂无分类" /> }}
        pagination={false}
        columns={[
          { title: '分类名称', dataIndex: 'name' },
          { title: '排序', dataIndex: 'sortOrder', width: 100 },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v) => (v === 1 ? <Tag color="green">启用</Tag> : <Tag color="default">停用</Tag>),
          },
          {
            title: '操作',
            key: 'op',
            width: 140,
            render: (_, r) => (
              <>
                <Button type="link" size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm title="确认删除该分类?" onConfirm={() => remove(r.id)}>
                  <Button type="link" size="small" danger>删除</Button>
                </Popconfirm>
              </>
            ),
          },
        ]}
      />

      <Modal
        title={editing ? '编辑分类' : '新增分类'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={submit}
        confirmLoading={saving}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="分类名称" rules={[{ required: true, message: '请输入分类名称' }]}>
            <Input placeholder="如 主食 / 饮品" />
          </Form.Item>
          <Form.Item name="sortOrder" label="排序(数字越小越靠前)">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
