// 用户端首页: 商家列表(公开浏览,营业中优先)
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, List, Tag, Input, Space, Pagination, Typography, Empty, Rate } from 'antd';
import { ShopOutlined, EnvironmentOutlined } from '@ant-design/icons';
import { merchantBrowseApi } from '../../api/merchants.js';
import { formatMoney } from '../../utils/format.js';

export default function HomePage() {
  const navigate = useNavigate();
  const [zone, setZone] = useState('');
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [data, setData] = useState({ records: [], total: 0 });
  const [loading, setLoading] = useState(false);

  const load = async (p = page, z = zone) => {
    setLoading(true);
    try {
      const res = await merchantBrowseApi.page({ zone: z || undefined, page: p, size });
      setData(res || { records: [], total: 0 });
    } catch (e) {
      setData({ records: [], total: 0 });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(1, zone);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSearch = () => {
    setPage(1);
    load(1, zone);
  };

  return (
    <div>
      <Typography.Title level={4}>选择商家</Typography.Title>
      <Space style={{ marginBottom: 16 }}>
        <Input.Search
          placeholder="按校区筛选"
          allowClear
          value={zone}
          onChange={(e) => setZone(e.target.value)}
          onSearch={onSearch}
          style={{ width: 240 }}
        />
      </Space>
      <List
        loading={loading}
        dataSource={data.records}
        locale={{ emptyText: <Empty description="暂无商家" /> }}
        renderItem={(m) => (
          <Card
            hoverable
            style={{ marginBottom: 12 }}
            onClick={() => navigate(`/merchants/${m.id}`)}
          >
            <List.Item.Meta
              avatar={<ShopOutlined style={{ fontSize: 40, color: '#fa8c16' }} />}
              title={
                <Space>
                  <span>{m.name}</span>
                  {m.isOpen === 1 ? <Tag color="green">营业中</Tag> : <Tag color="default">已打烊</Tag>}
                </Space>
              }
              description={
                <Space direction="vertical" size={4}>
                  <span>{m.description}</span>
                  <Space size="middle">
                    <span><EnvironmentOutlined /> {m.campusZone}</span>
                    <span>起送 {formatMoney(m.minOrderAmount)}</span>
                    <span>配送费 {formatMoney(m.deliveryFee)}</span>
                    <span>营业 {m.openTime}-{m.closeTime}</span>
                  </Space>
                  <span>
                    <Rate disabled allowHalf value={Number(m.rating) || 0} style={{ fontSize: 12 }} />
                    ({m.ratingCount || 0})
                  </span>
                </Space>
              }
            />
          </Card>
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
    </div>
  );
}
