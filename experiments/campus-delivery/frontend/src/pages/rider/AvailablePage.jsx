// 骑手: 待接单池
import { useEffect, useState } from 'react';
import { Card, Table, Button, Tag, message, Empty } from 'antd';
import { riderApi } from '../../api/rider.js';
import { formatDateTime, deliveryStatusText } from '../../utils/format.js';

export default function RiderAvailablePage() {
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(false);
  const [accepting, setAccepting] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const res = await riderApi.available();
      setList(res || []);
    } catch (e) {
      message.error(e.message || '加载待接单失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const accept = async (id) => {
    setAccepting(id);
    try {
      await riderApi.accept(id);
      message.success('接单成功');
      load();
    } catch (e) {
      message.error(e.message || '接单失败');
    } finally {
      setAccepting(null);
    }
  };

  return (
    <Card title="待接单池">
      <Table
        rowKey="id"
        loading={loading}
        dataSource={list}
        locale={{ emptyText: <Empty description="当前无待接单任务" /> }}
        pagination={false}
        columns={[
          { title: '订单号', dataIndex: 'orderNo', ellipsis: true },
          { title: '商家', dataIndex: 'merchantName' },
          { title: '取餐地址', dataIndex: 'pickupAddress', ellipsis: true },
          { title: '送达地址', dataIndex: 'deliveryAddress', ellipsis: true },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v) => <Tag color="orange">{deliveryStatusText(v)}</Tag>,
          },
          { title: '发布时间', dataIndex: 'createdAt', width: 160, render: (v) => formatDateTime(v) },
          {
            title: '操作',
            key: 'op',
            width: 100,
            render: (_, r) => (
              <Button type="primary" size="small" loading={accepting === r.id} onClick={() => accept(r.id)}>
                接单
              </Button>
            ),
          },
        ]}
      />
    </Card>
  );
}
