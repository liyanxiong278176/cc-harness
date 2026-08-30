// 商家详情页: 店铺信息 + 分类菜单 + 加购
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Card, Tag, Space, Button, Empty, Spin, Rate, message, Tabs } from 'antd';
import { ShopOutlined, ShoppingCartOutlined, EnvironmentOutlined } from '@ant-design/icons';
import { merchantBrowseApi } from '../../api/merchants.js';
import { cartApi } from '../../api/cart.js';
import { formatMoney } from '../../utils/format.js';

export default function MerchantDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [menu, setMenu] = useState(null);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await merchantBrowseApi.menu(id);
      setMenu(res);
    } catch (e) {
      message.error(e.message || '加载菜单失败');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const addToCart = async (dish, disabled) => {
    if (disabled) return;
    setAdding(dish.id);
    try {
      await cartApi.addItem({ dishId: dish.id, quantity: 1 });
      message.success('已加入购物车');
    } catch (e) {
      message.error(e.message || '加购失败');
    } finally {
      setAdding(null);
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', paddingTop: 80 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!menu) {
    return <Empty description="商家不存在" />;
  }

  const { merchant, categories } = menu;

  return (
    <div>
      <Card style={{ marginBottom: 16 }}>
        <Space direction="vertical" size={8}>
          <Space>
            <ShopOutlined style={{ fontSize: 32, color: '#fa8c16' }} />
            <span style={{ fontSize: 22, fontWeight: 700 }}>{merchant.name}</span>
            {merchant.isOpen === 1 ? <Tag color="green">营业中</Tag> : <Tag color="default">已打烊</Tag>}
          </Space>
          <span>{merchant.description}</span>
          <Space size="middle">
            <span><EnvironmentOutlined /> {merchant.campusZone}</span>
            <span>起送 {formatMoney(merchant.minOrderAmount)}</span>
            <span>配送费 {formatMoney(merchant.deliveryFee)}</span>
            <span>营业 {merchant.openTime}-{merchant.closeTime}</span>
          </Space>
          <span>
            <Rate disabled allowHalf value={Number(merchant.rating) || 0} /> ({merchant.ratingCount || 0})
          </span>
        </Space>
      </Card>

      <Tabs
        items={(categories || []).map((c) => ({
          key: String(c.id),
          label: c.name,
          children: (
            <Space direction="vertical" style={{ width: '100%' }}>
              {(c.dishes || []).length === 0 && <Empty description="暂无菜品" />}
              {(c.dishes || []).map((dish) => {
                const off = dish.status !== 1;
                const noStock = Number(dish.stock || 0) <= 0;
                return (
                  <Card key={dish.id} size="small" style={{ marginBottom: 8 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <Space direction="vertical" size={2}>
                        <span style={{ fontWeight: 600 }}>{dish.name}</span>
                        <span className="text-secondary">{dish.description}</span>
                        <Space size="middle">
                          <span className="money">{formatMoney(dish.price)}</span>
                          {dish.originalPrice && (
                            <span className="text-secondary" style={{ textDecoration: 'line-through' }}>
                              {formatMoney(dish.originalPrice)}
                            </span>
                          )}
                          <span>库存 {dish.stock}</span>
                          {dish.soldCount > 0 && <span>已售 {dish.soldCount}</span>}
                        </Space>
                      </Space>
                      <Button
                        type="primary"
                        icon={<ShoppingCartOutlined />}
                        disabled={merchant.isOpen !== 1 || off || noStock}
                        loading={adding === dish.id}
                        onClick={() => addToCart(dish, merchant.isOpen !== 1 || off || noStock)}
                      >
                        {off ? '已下架' : noStock ? '售罄' : '加购'}
                      </Button>
                    </div>
                  </Card>
                );
              })}
            </Space>
          ),
        }))}
      />

      <div style={{ position: 'fixed', right: 24, bottom: 24 }}>
        <Button type="primary" size="large" icon={<ShoppingCartOutlined />} onClick={() => navigate('/cart')}>
          去购物车
        </Button>
      </div>
    </div>
  );
}
