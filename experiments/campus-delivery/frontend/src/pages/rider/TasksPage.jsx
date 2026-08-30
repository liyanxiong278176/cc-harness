// 骑手: 我的配送任务
import { useEffect, useState } from 'react';
import { Card, Table, Tag, Tabs, Button, Popconfirm, message, Empty } from 'antd';
import { riderApi } from '../../api/rider.js';
import { formatDateTime, deliveryStatusText } from '../../utils/format.js';

const STATUS_TABS = [
  { key: 'ALL', label: '全部' },
  { key: 'ACCEPTED', label: '已接单' },
  { key: 'PICKED', label: '已取餐' },
  { key: 'DELIVERING', label: '配送中' },
  { key: 'DELIVERED', label: '已送达' },
];

export default function RiderTasksPage() {
  const [status, setStatus] = useState('ALL');
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);

  const load = async (p = page, s = status) => {
    setLoading(true);
    try {
      const res = await riderApi.tasks({
        status: s === 'ALL' ? undefined : s,
        page: p,
        size,
      });
      setData(res || { records: [], total: 0 });
    } catch (e) {
      message.error(e.message || '加载任务失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const act = async (fn, id, msg) => {
    try {
      await fn(id);
      message.success(msg);
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    }
  };

  return (
    <Card title="我的配送任务">
      <Tabs
        activeKey={status}
        onChange={(k) => {
          setStatus(k);
          setPage(1);
        }}
        items={STATUS_TABS}
      />
      <Table
        rowKey="id"
        loading={loading}
        dataSource={data.records}
        locale={{ emptyText: <Empty description="暂无任务" /> }}
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
          { title: '订单号', dataIndex: 'orderNo', ellipsis: true },
          { title: '商家', dataIndex: 'merchantName' },
          { title: '取餐地址', dataIndex: 'pickupAddress', ellipsis: true },
          { title: '送达地址', dataIndex: 'deliveryAddress', ellipsis: true },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v) => <Tag color={v === 'DELIVERED' ? 'green' : 'blue'}>{deliveryStatusText(v)}</Tag>,
          },
          { title: '创建时间', dataIndex: 'createdAt', width: 160, render: (v) => formatDateTime(v) },
          {
            title: '操作',
            key: 'op',
            width: 120,
            render: (_, r) => (
              <>
                {r.status === 'ACCEPTED' && (
                  <Popconfirm title="确认已到店取餐?" onConfirm={() => act(riderApi.pickup, r.id, '已取餐')}>
                    <Button type="primary" size="small">取餐</Button>
                  </Popconfirm>
                )}
                {r.status === 'PICKED' && (
                  <Popconfirm title="确认已送达?" onConfirm={() => act(riderApi.deliver, r.id, '已送达')}>
                    <Button type="primary" size="small">送达</Button>
                  </Popconfirm>
                )}
                {r.status === 'DELIVERING' && <Tag color="blue">配送中</Tag>}
                {r.status === 'DELIVERED' && <Tag color="green">已完成</Tag>}
              </>
            ),
          },
        ]}
      />
    </Card>
  );
}
