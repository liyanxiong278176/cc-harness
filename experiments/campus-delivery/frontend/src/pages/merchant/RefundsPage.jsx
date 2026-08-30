// 退款处理: 同意 / 拒绝
import { useEffect, useState } from 'react';
import {
  Card, Table, Button, Modal, Input, Popconfirm, Tag, message, Empty, Space,
} from 'antd';
import { merchantAdminApi } from '../../api/merchantAdmin.js';
import { formatMoney, formatDateTime, refundStatusText } from '../../utils/format.js';

export default function RefundsPage() {
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [rejectTarget, setRejectTarget] = useState(null);
  const [rejectReason, setRejectReason] = useState('');
  const [acting, setActing] = useState(false);

  const load = async (p = page) => {
    setLoading(true);
    try {
      const res = await merchantAdminApi.refunds({ page: p, size });
      setData(res || { records: [], total: 0 });
    } catch (e) {
      message.error(e.message || '加载退款单失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const approve = async (id) => {
    setActing(true);
    try {
      await merchantAdminApi.approveRefund(id);
      message.success('已同意退款');
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    } finally {
      setActing(false);
    }
  };

  const reject = async () => {
    setActing(true);
    try {
      await merchantAdminApi.rejectRefund(rejectTarget.id, rejectReason);
      message.success('已拒绝退款');
      setRejectTarget(null);
      setRejectReason('');
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    } finally {
      setActing(false);
    }
  };

  return (
    <Card title="退款处理">
      <Table
        rowKey="id"
        loading={loading}
        dataSource={data.records}
        locale={{ emptyText: <Empty description="暂无退款申请" /> }}
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
          { title: '金额', dataIndex: 'amount', width: 110, render: (v) => formatMoney(v) },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
          {
            title: '状态',
            dataIndex: 'status',
            width: 110,
            render: (v) => (
              <Tag color={v === 'PENDING' ? 'orange' : v === 'APPROVED' || v === 'REFUNDED' ? 'green' : 'default'}>
                {refundStatusText(v)}
              </Tag>
            ),
          },
          { title: '申请时间', dataIndex: 'createdAt', width: 160, render: (v) => formatDateTime(v) },
          {
            title: '操作',
            key: 'op',
            width: 180,
            render: (_, r) =>
              r.status === 'PENDING' ? (
                <Space>
                  <Popconfirm title="确认同意退款?" onConfirm={() => approve(r.id)}>
                    <Button type="primary" size="small" loading={acting}>同意</Button>
                  </Popconfirm>
                  <Button danger size="small" onClick={() => { setRejectTarget(r); setRejectReason(''); }}>
                    拒绝
                  </Button>
                </Space>
              ) : <Tag color="default">已处理</Tag>,
          },
        ]}
      />

      <Modal
        title="拒绝退款"
        open={!!rejectTarget}
        onCancel={() => setRejectTarget(null)}
        onOk={reject}
        confirmLoading={acting}
      >
        <Input.TextArea rows={3} placeholder="拒绝原因" value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} />
      </Modal>
    </Card>
  );
}
