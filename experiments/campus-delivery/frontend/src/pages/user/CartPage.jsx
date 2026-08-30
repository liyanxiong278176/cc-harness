// 购物车页: 按店铺分组,数量/勾选/删除,去结算
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Table, InputNumber, Checkbox, Button, Space, Popconfirm, message, Empty, Tag, Spin } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { cartApi } from '../../api/cart.js';
import { formatMoney } from '../../utils/format.js';

export default function CartPage() {
  const navigate = useNavigate();
  const [cart, setCart] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await cartApi.get();
      setCart(res);
    } catch (e) {
      message.error(e.message || '加载购物车失败');
      setCart({ groups: [], totalAmount: 0, totalCheckedCount: 0 });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const reload = async () => {
    await load();
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', paddingTop: 80 }}>
        <Spin size="large" />
      </div>
    );
  }

  const groups = cart?.groups || [];
  const totalAmount = Number(cart?.totalAmount || 0);
  const totalCheckedCount = cart?.totalCheckedCount || 0;
  const empty = groups.every((g) => (g.items || []).length === 0);

  if (empty) {
    return (
      <Empty description="购物车为空" style={{ paddingTop: 80 }}>
        <Button type="primary" onClick={() => navigate('/')}>去点餐</Button>
      </Empty>
    );
  }

  return (
    <div>
      <Space direction="vertical" style={{ width: '100%' }}>
        {groups.map((g) => (
          <Card
            key={g.merchantId}
            title={
              <Space>
                <span>{g.merchantName}</span>
                {g.isOpen === 1 ? <Tag color="green">营业中</Tag> : <Tag color="default">已打烊</Tag>}
              </Space>
            }
          >
            <Table
              rowKey="dishId"
              size="small"
              pagination={false}
              dataSource={g.items || []}
              columns={[
                {
                  title: '菜品',
                  dataIndex: 'dishName',
                  render: (v, it) => (
                    <Space>
                      <Checkbox
                        checked={it.checked === 1}
                        disabled={g.isOpen !== 1}
                        onChange={async (e) => {
                          try {
                            await cartApi.updateChecked(it.dishId, e.target.checked ? 1 : 0);
                            await reload();
                          } catch (err) {
                            message.error(err.message || '操作失败');
                          }
                        }}
                      />
                      <span>{v}</span>
                    </Space>
                  ),
                },
                {
                  title: '单价',
                  dataIndex: 'price',
                  width: 100,
                  render: (v) => formatMoney(v),
                },
                {
                  title: '数量',
                  dataIndex: 'quantity',
                  width: 140,
                  render: (v, it) => (
                    <InputNumber
                      min={1}
                      max={it.stock || 99}
                      value={v}
                      disabled={g.isOpen !== 1}
                      onChange={async (q) => {
                        if (!q) return;
                        try {
                          await cartApi.updateQuantity(it.dishId, q);
                          await reload();
                        } catch (err) {
                          message.error(err.message || '修改数量失败');
                        }
                      }}
                    />
                  ),
                },
                {
                  title: '小计',
                  key: 'subtotal',
                  width: 110,
                  render: (_, it) => (
                    <span className="money">{formatMoney(Number(it.price || 0) * Number(it.quantity || 0))}</span>
                  ),
                },
                {
                  title: '操作',
                  key: 'op',
                  width: 80,
                  render: (_, it) => (
                    <Popconfirm
                      title="确认删除该商品?"
                      onConfirm={async () => {
                        try {
                          await cartApi.removeItem(it.dishId);
                          message.success('已删除');
                          await reload();
                        } catch (err) {
                          message.error(err.message || '删除失败');
                        }
                      }}
                    >
                      <Button type="text" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  ),
                },
              ]}
            />
          </Card>
        ))}
      </Space>

      <div
        style={{
          position: 'fixed',
          left: 0,
          right: 0,
          bottom: 0,
          background: '#fff',
          padding: '12px 24px',
          boxShadow: '0 -2px 8px rgba(0,0,0,0.08)',
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          gap: 24,
        }}
      >
        <Space size="large">
          <span>已选 {totalCheckedCount} 件</span>
          <span>
            合计: <span className="money" style={{ fontSize: 20 }}>{formatMoney(totalAmount)}</span>
          </span>
          <Button
            type="primary"
            size="large"
            disabled={totalCheckedCount <= 0}
            onClick={() => navigate('/checkout')}
          >
            去结算
          </Button>
        </Space>
      </div>
    </div>
  );
}
