// 订单详情: 明细 + 按状态操作(支付/取消/退款/评价)
import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Card, Descriptions, Tag, Space, Button, Modal, Input, message, Spin, Empty, Rate, Divider,
} from 'antd';
import { orderApi } from '../../api/orders.js';
import { formatMoney, formatDateTime, orderStatusText, deliveryStatusText } from '../../utils/format.js';

const { TextArea } = Input;

export default function OrderDetailPage() {
  const { orderNo } = useParams();
  const [order, setOrder] = useState(null);
  const [loading, setLoading] = useState(true);
  const [track, setTrack] = useState(null);
  const [showTrack, setShowTrack] = useState(false);

  // 操作 Modal 状态
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelReason, setCancelReason] = useState('');
  const [refundOpen, setRefundOpen] = useState(false);
  const [refundReason, setRefundReason] = useState('');
  const [reviewOpen, setReviewOpen] = useState(false);
  const [rating, setRating] = useState(5);
  const [reviewContent, setReviewContent] = useState('');
  const [acting, setActing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await orderApi.detail(orderNo);
      setOrder(res);
    } catch (e) {
      message.error(e.message || '加载订单失败');
    } finally {
      setLoading(false);
    }
  }, [orderNo]);

  useEffect(() => {
    load();
  }, [load]);

  const reload = async () => {
    await load();
    setCancelOpen(false);
    setRefundOpen(false);
    setReviewOpen(false);
  };

  const doCancel = async () => {
    setActing(true);
    try {
      await orderApi.cancel(orderNo, cancelReason);
      message.success('订单已取消');
      await reload();
    } catch (e) {
      message.error(e.message || '取消失败');
    } finally {
      setActing(false);
    }
  };

  const doPay = async () => {
    setActing(true);
    try {
      const res = await orderApi.pay(orderNo, 'MOCK');
      message.success('支付成功');
      await reload();
    } catch (e) {
      message.error(e.message || '支付失败');
    } finally {
      setActing(false);
    }
  };

  const doRefund = async () => {
    setActing(true);
    try {
      await orderApi.refund(orderNo, refundReason);
      message.success('退款申请已提交');
      await reload();
    } catch (e) {
      message.error(e.message || '申请失败');
    } finally {
      setActing(false);
    }
  };

  const doReview = async () => {
    setActing(true);
    try {
      await orderApi.review(orderNo, { rating, content: reviewContent });
      message.success('评价成功');
      await reload();
    } catch (e) {
      message.error(e.message || '评价失败');
    } finally {
      setActing(false);
    }
  };

  const loadTrack = async () => {
    setShowTrack(true);
    try {
      const res = await orderApi.track(orderNo);
      setTrack(res);
    } catch (e) {
      setTrack(null);
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', paddingTop: 80 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!order) {
    return <Empty description="订单不存在" />;
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card title={<Space>订单 {order.orderNo} <Tag color="blue">{orderStatusText(order.status)}</Tag></Space>}>
        <Descriptions column={2} size="small">
          <Descriptions.Item label="商家">{order.merchantName}</Descriptions.Item>
          <Descriptions.Item label="下单时间">{formatDateTime(order.createdAt)}</Descriptions.Item>
          <Descriptions.Item label="商品金额">{formatMoney(order.totalAmount)}</Descriptions.Item>
          <Descriptions.Item label="优惠">{formatMoney(order.discountAmount)}</Descriptions.Item>
          <Descriptions.Item label="配送费">{formatMoney(order.deliveryFee)}</Descriptions.Item>
          <Descriptions.Item label="实付">{formatMoney(order.payAmount)}</Descriptions.Item>
          {order.payTime && <Descriptions.Item label="支付时间">{formatDateTime(order.payTime)}</Descriptions.Item>}
          {order.payChannel && <Descriptions.Item label="支付渠道">{order.payChannel}</Descriptions.Item>}
          {order.remark && <Descriptions.Item label="备注" span={2}>{order.remark}</Descriptions.Item>}
        </Descriptions>

        <Divider style={{ margin: '12px 0' }} />
        {(order.items || []).map((it, i) => (
          <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
            <span>{it.dishName} × {it.quantity}</span>
            <span>{formatMoney(Number(it.price || 0) * Number(it.quantity || 0))}</span>
          </div>
        ))}

        <div style={{ marginTop: 16 }}>
          <Space>
            {order.status === 'CREATED' && (
              <>
                <Button type="primary" loading={acting} onClick={doPay}>去支付</Button>
                <Button danger onClick={() => setCancelOpen(true)}>取消订单</Button>
              </>
            )}
            {order.status === 'PAID' && (
              <Button danger onClick={() => setRefundOpen(true)}>申请退款</Button>
            )}
            {order.status === 'COMPLETED' && !order.reviewed && (
              <Button type="primary" onClick={() => setReviewOpen(true)}>评价</Button>
            )}
            <Button onClick={loadTrack}>查看配送轨迹</Button>
          </Space>
        </div>
      </Card>

      {showTrack && (
        <Card title="配送跟踪">
          {track ? (
            <Descriptions column={1} size="small">
              <Descriptions.Item label="订单状态">{orderStatusText(track.orderStatus)}</Descriptions.Item>
              <Descriptions.Item label="支付状态">
                {track.payStatus ? (track.payStatus === 'SUCCESS' ? '已支付' : track.payStatus) : '未支付'}
              </Descriptions.Item>
              <Descriptions.Item label="配送状态">{deliveryStatusText(track.deliveryStatus)}</Descriptions.Item>
              <Descriptions.Item label="下单时间">{formatDateTime(track.createdAt)}</Descriptions.Item>
              <Descriptions.Item label="支付时间">{formatDateTime(track.payTime)}</Descriptions.Item>
              <Descriptions.Item label="取消时间">{formatDateTime(track.cancelTime)}</Descriptions.Item>
              <Descriptions.Item label="送达时间">{formatDateTime(track.deliveredTime)}</Descriptions.Item>
            </Descriptions>
          ) : (
            <Empty description="暂无轨迹" />
          )}
        </Card>
      )}

      {/* 取消订单 */}
      <Modal
        title="取消订单"
        open={cancelOpen}
        onCancel={() => setCancelOpen(false)}
        onOk={doCancel}
        confirmLoading={acting}
      >
        <TextArea rows={2} placeholder="取消原因(可选)" value={cancelReason} onChange={(e) => setCancelReason(e.target.value)} />
      </Modal>

      {/* 申请退款 */}
      <Modal
        title="申请退款"
        open={refundOpen}
        onCancel={() => setRefundOpen(false)}
        onOk={doRefund}
        confirmLoading={acting}
      >
        <TextArea rows={2} placeholder="退款原因" value={refundReason} onChange={(e) => setRefundReason(e.target.value)} />
      </Modal>

      {/* 评价 */}
      <Modal
        title="评价订单"
        open={reviewOpen}
        onCancel={() => setReviewOpen(false)}
        onOk={doReview}
        confirmLoading={acting}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Rate value={rating} onChange={setRating} />
          <TextArea rows={3} placeholder="评价内容" value={reviewContent} onChange={(e) => setReviewContent(e.target.value)} />
        </Space>
      </Modal>
    </Space>
  );
}
