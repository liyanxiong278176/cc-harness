// 我的订单列表: 状态 Tab + 分页
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Table, Tag, Tabs, Button, message, Empty } from 'antd';
import { orderApi } from '../../api/orders.js';
import { formatMoney, formatDateTime, orderStatusText } from '../../utils/format.js';

const STATUS_TABS = [
  { key: 'ALL', label: '全部' },
  { key: 'CREATED', label: '待支付' },
  { key: 'PAID', label: '已支付' },
  { key: 'PREPARING', label: '备餐中' },
  { key: 'DELIVERING', label: '配送中' },
  { key: 'COMPLETED', label: '已完成' },
  { key: 'CANCELLED', label: '已取消' },
  { key: 'REFUND', label: '退款' },
];

export default function OrdersPage() {
  const navigate = useNavigate();
  const [status, setStatus] = useState('ALL');
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (p = page, s = status) => {
    setLoading(true);
    try {
      const params = { page: p, size };
      if (s === 'REFUND') {
        params.status = 'REFUNDING';
      } else if (s !== 'ALL') {
        params.status = s;
      }
      const res = await orderApi.page(params);
      setData(res || { records: [], total: 0 });
    } catch (e) {
      message.error(e.message || '加载订单失败');
      setData({ records: [], total: 0 });
    } finally {
      setLoading(false);
    }
  }, [page, size, status]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  return (
    <div>
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
        locale={{ emptyText: <Empty description="暂无订单" /> }}
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
          {
            title: '状态',
            dataIndex: 'status',
            width: 110,
            render: (v) => <Tag color={v === 'CANCELLED' ? 'default' : v === 'COMPLETED' ? 'green' : 'blue'}>{orderStatusText(v)}</Tag>,
          },
          { title: '金额', dataIndex: 'payAmount', width: 110, render: (v) => formatMoney(v) },
          { title: '下单时间', dataIndex: 'createdAt', width: 160, render: (v) => formatDateTime(v) },
          {
            title: '操作',
            key: 'op',
            width: 100,
            render: (_, r) => (
              <Button type="link" onClick={() => navigate(`/orders/${r.orderNo}`)}>查看详情</Button>
            ),
          },
        ]}
      />
    </div>
  );
}
