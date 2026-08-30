// 商家订单管理: 接单 / 出餐完成
import { useEffect, useState } from 'react';
import { Card, Table, Tag, Tabs, Button, message, Empty, Popconfirm } from 'antd';
import { merchantAdminApi } from '../../api/merchantAdmin.js';
import { formatMoney, formatDateTime, orderStatusText } from '../../utils/format.js';

const STATUS_TABS = [
  { key: 'ALL', label: '全部' },
  { key: 'CREATED', label: '待支付' },
  { key: 'PAID', label: '待接单' },
  { key: 'PREPARING', label: '备餐中' },
  { key: 'DELIVERING', label: '配送中' },
  { key: 'COMPLETED', label: '已完成' },
  { key: 'CANCELLED', label: '已取消' },
];

export default function MerchantOrdersPage() {
  const [status, setStatus] = useState('ALL');
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);

  const load = async (p = page, s = status) => {
    setLoading(true);
    try {
      const res = await merchantAdminApi.orders({
        status: s === 'ALL' ? undefined : s,
        page: p,
        size,
      });
      setData(res || { records: [], total: 0 });
    } catch (e) {
      message.error(e.message || '加载订单失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const accept = async (orderNo) => {
    try {
      await merchantAdminApi.acceptOrder(orderNo);
      message.success('已接单');
      load();
    } catch (e) {
      message.error(e.message || '接单失败');
    }
  };

  const ready = async (orderNo) => {
    try {
      await merchantAdminApi.readyOrder(orderNo);
      message.success('已出餐完成,等待骑手');
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    }
  };

  return (
    <Card title="订单管理">
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
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v) => <Tag>{orderStatusText(v)}</Tag>,
          },
          { title: '金额', dataIndex: 'payAmount', width: 110, render: (v) => formatMoney(v) },
          { title: '备注', dataIndex: 'remark', ellipsis: true },
          { title: '下单时间', dataIndex: 'createdAt', width: 160, render: (v) => formatDateTime(v) },
          {
            title: '操作',
            key: 'op',
            width: 200,
            render: (_, r) => (
              <>
                {r.status === 'CREATED' && (
                  <Popconfirm title="确认接单?" onConfirm={() => accept(r.orderNo)}>
                    <Button type="primary" size="small">接单</Button>
                  </Popconfirm>
                )}
                {r.status === 'PAID' && (
                  <Popconfirm title="确认已出餐?" onConfirm={() => ready(r.orderNo)}>
                    <Button type="primary" size="small">出餐完成</Button>
                  </Popconfirm>
                )}
                {(r.status === 'PREPARING' || r.status === 'DELIVERING') && (
                  <Tag color="blue">备餐/配送中</Tag>
                )}
                {(r.status === 'COMPLETED' || r.status === 'CANCELLED') && <Tag color="default">已完结</Tag>}
              </>
            ),
          },
        ]}
      />
    </Card>
  );
}
