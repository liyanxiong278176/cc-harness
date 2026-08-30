// 评价管理: 列表 + 回复
import { useEffect, useState } from 'react';
import { Card, Table, Button, Modal, Input, Rate, message, Empty, Space, Tag } from 'antd';
import { merchantAdminApi } from '../../api/merchantAdmin.js';
import { formatDateTime } from '../../utils/format.js';

export default function ReviewsPage() {
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [replyTarget, setReplyTarget] = useState(null);
  const [replyText, setReplyText] = useState('');
  const [replying, setReplying] = useState(false);

  const load = async (p = page) => {
    setLoading(true);
    try {
      const res = await merchantAdminApi.reviews({ page: p, size });
      setData(res || { records: [], total: 0 });
    } catch (e) {
      message.error(e.message || '加载评价失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitReply = async () => {
    setReplying(true);
    try {
      await merchantAdminApi.replyReview(replyTarget.id, replyText);
      message.success('回复成功');
      setReplyTarget(null);
      setReplyText('');
      load();
    } catch (e) {
      message.error(e.message || '回复失败');
    } finally {
      setReplying(false);
    }
  };

  return (
    <Card title="评价管理">
      <Table
        rowKey="id"
        loading={loading}
        dataSource={data.records}
        locale={{ emptyText: <Empty description="暂无评价" /> }}
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
          { title: '用户', dataIndex: 'userName', width: 120 },
          {
            title: '评分',
            dataIndex: 'rating',
            width: 160,
            render: (v) => <Rate disabled allowHalf value={Number(v) || 0} style={{ fontSize: 12 }} />,
          },
          { title: '内容', dataIndex: 'content' },
          { title: '订单号', dataIndex: 'orderNo', width: 200, ellipsis: true },
          { title: '时间', dataIndex: 'createdAt', width: 160, render: (v) => formatDateTime(v) },
          {
            title: '回复',
            key: 'reply',
            width: 140,
            render: (_, r) => (
              r.reply ? <Tag color="green">{r.reply}</Tag>
                : <Button type="link" size="small" onClick={() => { setReplyTarget(r); setReplyText(''); }}>回复</Button>
            ),
          },
        ]}
      />

      <Modal
        title="回复评价"
        open={!!replyTarget}
        onCancel={() => setReplyTarget(null)}
        onOk={submitReply}
        confirmLoading={replying}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <span className="text-secondary">原始评价: {replyTarget?.content}</span>
          <Input.TextArea rows={3} placeholder="回复内容" value={replyText} onChange={(e) => setReplyText(e.target.value)} />
        </Space>
      </Modal>
    </Card>
  );
}
