// 商家工作台: 核心指标
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Row, Col, Statistic, Button, Space, message, Spin } from 'antd';
import {
  ShoppingOutlined, MoneyCollectOutlined, ClockCircleOutlined, RollbackOutlined, ShopOutlined,
} from '@ant-design/icons';
import { merchantAdminApi } from '../../api/merchantAdmin.js';
import { formatMoney } from '../../utils/format.js';

export default function DashboardPage() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    merchantAdminApi.dashboard()
      .then(setData)
      .catch((e) => message.error(e.message || '加载工作台失败'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', paddingTop: 80 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!data) return null;

  const d = data;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="今日订单数" value={d.todayOrderCount || 0} prefix={<ShoppingOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="今日营业额" value={formatMoney(d.todayAmount)} prefix={<MoneyCollectOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="本月营业额" value={formatMoney(d.monthAmount)} prefix={<MoneyCollectOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="在售菜品" value={d.totalDishCount || 0} prefix={<ShopOutlined />} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="待接单"
              value={d.pendingAcceptCount || 0}
              prefix={<ClockCircleOutlined style={{ color: '#faad14' }} />}
            />
            <Button type="link" onClick={() => navigate('/merchant/orders')}>去处理</Button>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="待退款"
              value={d.pendingRefundCount || 0}
              prefix={<RollbackOutlined style={{ color: '#ff4d4f' }} />}
            />
            <Button type="link" onClick={() => navigate('/merchant/refunds')}>去处理</Button>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
