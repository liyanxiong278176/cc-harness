// 菜品管理: 增删改 + 上下架 + 库存
import { useEffect, useState } from 'react';
import {
  Card, Table, Button, Modal, Form, Input, InputNumber, Select, Switch, Tag, message, Empty, Space, Popconfirm,
} from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { merchantAdminApi } from '../../api/merchantAdmin.js';
import { formatMoney } from '../../utils/format.js';

export default function DishesPage() {
  const [categories, setCategories] = useState([]);
  const [categoryId, setCategoryId] = useState();
  const [status, setStatus] = useState();
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const load = async (p = page, c = categoryId, s = status) => {
    setLoading(true);
    try {
      const res = await merchantAdminApi.dishes({
        categoryId: c || undefined,
        status: s === undefined ? undefined : s,
        page: p,
        size,
      });
      setData(res || { records: [], total: 0 });
    } catch (e) {
      message.error(e.message || '加载菜品失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    merchantAdminApi.categories()
      .then((res) => setCategories(res || []))
      .catch(() => setCategories([]));
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openAdd = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ price: 0, originalPrice: 0, stock: 0, status: 1 });
    setModalOpen(true);
  };

  const openEdit = (row) => {
    setEditing(row);
    form.setFieldsValue({
      categoryId: row.categoryId,
      name: row.name,
      description: row.description,
      price: row.price,
      originalPrice: row.originalPrice,
      stock: row.stock,
      image: row.image,
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        await merchantAdminApi.updateDish(editing.id, values);
      } else {
        await merchantAdminApi.addDish(values);
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

  const setStock = async (id, stock) => {
    setStocking(id);
    try {
      await merchantAdminApi.setStock(id, stock);
      message.success('库存已更新');
      load();
    } catch (e) {
      message.error(e.message || '更新库存失败');
    } finally {
      setStocking(null);
    }
  };

  const toggleStatus = async (row, checked) => {
    try {
      await merchantAdminApi.setStatus(row.id, checked ? 1 : 0);
      message.success(checked ? '已上架' : '已下架');
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    }
  };

  return (
    <Card
      title="菜品管理"
      extra={
        <Space>
          <Select
            allowClear
            placeholder="全部分类"
            style={{ width: 140 }}
            value={categoryId}
            onChange={(v) => {
              setCategoryId(v);
              setPage(1);
              load(1, v, status);
            }}
            options={categories.map((c) => ({ value: c.id, label: c.name }))}
          />
          <Select
            allowClear
            placeholder="全部状态"
            style={{ width: 120 }}
            value={status}
            onChange={(v) => {
              setStatus(v);
              setPage(1);
              load(1, categoryId, v);
            }}
            options={[
              { value: 1, label: '上架' },
              { value: 0, label: '下架' },
            ]}
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={openAdd}>新增菜品</Button>
        </Space>
      }
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={data.records}
        locale={{ emptyText: <Empty description="暂无菜品" /> }}
        pagination={{
          current: page,
          pageSize: size,
          total: data.total,
          showSizeChanger: false,
          onChange: (p) => {
            setPage(p);
            load(p);
          },
        }}
        columns={[
          { title: '菜品', dataIndex: 'name' },
          {
            title: '分类',
            key: 'category',
            width: 100,
            render: (_, r) => categories.find((c) => c.id === r.categoryId)?.name || `#${r.categoryId}`,
          },
          { title: '价格', dataIndex: 'price', width: 100, render: (v) => formatMoney(v) },
          {
            title: '库存',
            dataIndex: 'stock',
            width: 160,
            render: (v, r) => (
              <InputNumber
                size="small"
                min={0}
                value={v}
                onBlur={(e) => {
                  const nv = Number(e.target.value);
                  if (nv !== v) setStock(r.id, nv);
                }}
              />
            ),
          },
          { title: '已售', dataIndex: 'soldCount', width: 80 },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v, r) => (
              <Switch
                size="small"
                checked={v === 1}
                checkedChildren="上架"
                unCheckedChildren="下架"
                onChange={(c) => toggleStatus(r, c)}
              />
            ),
          },
          {
            title: '操作',
            key: 'op',
            width: 140,
            render: (_, r) => (
              <>
                <Button type="link" size="small" onClick={() => openEdit(r)}>编辑</Button>
                <Popconfirm
                  title="删除菜品?(不可恢复)"
                  onConfirm={async () => {
                    try {
                      await merchantAdminApi.updateDish(r.id, { ...r, status: 0 });
                      message.success('已下架处理');
                      load();
                    } catch (e) {
                      message.error(e.message || '操作失败');
                    }
                  }}
                >
                  <Button type="link" size="small" danger>下架</Button>
                </Popconfirm>
              </>
            ),
          },
        ]}
      />

      <Modal
        title={editing ? '编辑菜品' : '新增菜品'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={submit}
        confirmLoading={saving}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="categoryId" label="分类" rules={[{ required: true, message: '请选择分类' }]}>
            <Select
              placeholder="选择分类"
              options={categories.map((c) => ({ value: c.id, label: c.name }))}
            />
          </Form.Item>
          <Form.Item name="name" label="菜品名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="image" label="图片URL">
            <Input placeholder="https://..." />
          </Form.Item>
          <Form.Item name="price" label="售价(元)" rules={[{ required: true }]}>
            <InputNumber min={0} step={0.5} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="originalPrice" label="原价(元,可选)">
            <InputNumber min={0} step={0.5} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="stock" label="库存" rules={[{ required: true }]}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
