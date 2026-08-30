// 结算页: 选择地址 + 优惠券 + 提交订单
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Radio, Button, Space, Input, message, Spin, Empty, Typography, Tag } from 'antd';
import { userApi } from '../../api/user.js';
import { cartApi } from '../../api/cart.js';
import { orderApi } from '../../api/orders.js';
import { formatMoney, couponTypeText } from '../../utils/format.js';

export default function CheckoutPage() {
  const navigate = useNavigate();
  const [addresses, setAddresses] = useState([]);
  const [cart, setCart] = useState(null);
  const [coupons, setCoupons] = useState([]);
  const [addressId, setAddressId] = useState(null);
  const [couponId, setCouponId] = useState(null);
  const [remark, setRemark] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [addr, c, cps] = await Promise.all([
        userApi.listAddresses(),
        cartApi.get(),
        userApi.myCoupons({ status: 'UNUSED' }),
      ]);
      setAddresses(addr || []);
      setCart(c);
      setCoupons(cps || []);
      const def = (addr || []).find((a) => a.isDefault === 1);
      setAddressId((id) => id || (def ? def.id : (addr && addr[0] ? addr[0].id : null)));
    } catch (e) {
      message.error(e.message || '加载结算信息失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const groups = cart?.groups || [];
  const firstGroup = groups[0];
  const totalAmount = Number(cart?.totalAmount || 0);

  const submit = async () => {
    if (!firstGroup) {
      message.warning('购物车为空');
      return;
    }
    if (!addressId) {
      message.warning('请选择收货地址');
      return;
    }
    setSubmitting(true);
    try {
      const orderNo = await orderApi.checkout({
        merchantId: firstGroup.merchantId,
        addressId,
        couponId: couponId || undefined,
        remark: remark || undefined,
      });
      message.success('下单成功');
      navigate(`/orders/${orderNo}`);
    } catch (e) {
      message.error(e.message || '下单失败');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', paddingTop: 80 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!firstGroup) {
    return (
      <Empty description="购物车为空" style={{ paddingTop: 60 }}>
        <Button type="primary" onClick={() => navigate('/')}>去点餐</Button>
      </Empty>
    );
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Typography.Title level={4} style={{ margin: 0 }}>确认订单</Typography.Title>

      <Card title="收货地址">
        {addresses.length === 0 ? (
          <Empty description="暂无地址,请先添加" />
        ) : (
          <Radio.Group value={addressId} onChange={(e) => setAddressId(e.target.value)}>
            <Space direction="vertical">
              {addresses.map((a) => (
                <Radio key={a.id} value={a.id}>
                  {a.receiverName} {a.receiverPhone} - {a.campusZone} {a.detail}
                  {a.isDefault === 1 && <Tag color="blue" style={{ marginLeft: 8 }}>默认</Tag>}
                </Radio>
              ))}
            </Space>
          </Radio.Group>
        )}
      </Card>

      <Card title="商品清单">
        {(firstGroup.items || []).map((it) => (
          <div key={it.dishId} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0' }}>
            <span>{it.dishName} × {it.quantity}</span>
            <span>{formatMoney(Number(it.price || 0) * Number(it.quantity || 0))}</span>
          </div>
        ))}
        <div style={{ display: 'flex', justifyContent: 'space-between', paddingTop: 8 }}>
          <span className="text-secondary">{firstGroup.merchantName}</span>
          <span>商品合计 {formatMoney(totalAmount)}</span>
        </div>
      </Card>

      {coupons.length > 0 && (
        <Card title="优惠券">
          <Radio.Group value={couponId} onChange={(e) => setCouponId(e.target.value)}>
            <Space direction="vertical">
              <Radio value={null}>不使用优惠券</Radio>
              {coupons.map((c) => (
                <Radio key={c.id} value={c.id}>
                  {c.name}({couponTypeText(c.type)}) 满{formatMoney(c.thresholdAmount)}可用
                  {c.type === 'FULL_REDUCTION' && <> 减{formatMoney(c.discountAmount)}</>}
                </Radio>
              ))}
            </Space>
          </Radio.Group>
        </Card>
      )}

      <Card title="备注">
        <Input.TextArea
          rows={2}
          placeholder="口味、配送等备注(可选)"
          value={remark}
          onChange={(e) => setRemark(e.target.value)}
        />
      </Card>

      <div style={{ textAlign: 'right' }}>
        <Space size="large">
          <span>
            应付: <span className="money" style={{ fontSize: 20 }}>{formatMoney(totalAmount)}</span>
          </span>
          <Button type="primary" size="large" loading={submitting} onClick={submit}>
            提交订单
          </Button>
        </Space>
      </div>
    </Space>
  );
}
