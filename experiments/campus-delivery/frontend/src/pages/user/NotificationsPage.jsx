// 消息通知: 分页 + 已读
import { useCallback, useEffect, useState } from 'react';
import { Card, List, Button, Tag, Pagination, message, Empty, Space } from 'antd';
import { userApi } from '../../api/user.js';
import { formatDateTime, notificationTypeText } from '../../utils/format.js';

export default function NotificationsPage() {
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (p = page) => {
    setLoading(true);
    try {
      const res = await userApi.notifications({ page: p, size });
      setData(res || { records: [], total: 0 });
      const c = await userApi.unreadCount();
      setUnread(Number(c || 0));
    } catch (e) {
      message.error(e.message || '加载通知失败');
    } finally {
      setLoading(false);
    }
  }, [page, size]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const markRead = async (id) => {
    try {
      await userApi.markRead(id);
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    }
  };

  const markAllRead = async () => {
    try {
      await userApi.markAllRead();
      message.success('已全部标记已读');
      load();
    } catch (e) {
      message.error(e.message || '操作失败');
    }
  };

  return (
    <Card
      title={<>消息通知 {unread > 0 && <Tag color="red">未读 {unread}</Tag>}</>}
      extra={
        unread > 0 && (
          <Button size="small" onClick={markAllRead}>全部已读</Button>
        )
      }
    >
      <List
        loading={loading}
        dataSource={data.records}
        locale={{ emptyText: <Empty description="暂无通知" /> }}
        renderItem={(n) => (
          <List.Item
            onClick={() => !n.isRead && markRead(n.id)}
            style={{ cursor: n.isRead ? 'default' : 'pointer', background: n.isRead ? 'transparent' : '#e6f4ff' }}
          >
            <List.Item.Meta
              title={
                <Space>
                  {!n.isRead && <Tag color="red">未读</Tag>}
                  <Tag>{notificationTypeText(n.type)}</Tag>
                  <span>{n.title}</span>
                </Space>
              }
              description={
                <Space direction="vertical" size={2}>
                  <span>{n.content}</span>
                  <span className="text-secondary">{formatDateTime(n.createdAt)}</span>
                </Space>
              }
            />
          </List.Item>
        )}
      />
      <div style={{ textAlign: 'right', marginTop: 12 }}>
        <Pagination
          current={page}
          pageSize={size}
          total={data.total}
          showSizeChanger={false}
          onChange={(p) => {
            setPage(p);
            load(p);
          }}
        />
      </div>
    </Card>
  );
}
