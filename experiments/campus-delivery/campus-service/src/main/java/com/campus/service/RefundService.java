package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.model.PageQuery;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.entity.OrderItem;
import com.campus.dao.entity.RefundRecord;
import com.campus.dao.entity.StockChangeLog;
import com.campus.dao.mapper.DishMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.OrderItemMapper;
import com.campus.dao.mapper.RefundRecordMapper;
import com.campus.dao.mapper.StockChangeLogMapper;
import com.campus.service.adapter.PaymentGateway;
import com.campus.service.mq.OrderEventPublisher;
import com.campus.service.support.OrderStateMachine;
import com.campus.service.vo.RefundVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 退款服务(支付/退款域): 申请、商家审核、退款入账、库存/券回补。
 * 并发/幂等: 状态变更一律走「条件更新」(status 前置校验),重复审核影响行数为 0 直接返回。
 */
@Service
public class RefundService {

    private final RefundRecordMapper refundRecordMapper;
    private final OrderInfoMapper orderInfoMapper;
    private final OrderItemMapper orderItemMapper;
    private final DishMapper dishMapper;
    private final StockChangeLogMapper stockChangeLogMapper;
    private final CouponService couponService;
    private final PaymentGateway paymentGateway;
    private final OrderEventPublisher orderEventPublisher;

    public RefundService(RefundRecordMapper refundRecordMapper,
                         OrderInfoMapper orderInfoMapper,
                         OrderItemMapper orderItemMapper,
                         DishMapper dishMapper,
                         StockChangeLogMapper stockChangeLogMapper,
                         CouponService couponService,
                         PaymentGateway paymentGateway,
                         OrderEventPublisher orderEventPublisher) {
        this.refundRecordMapper = refundRecordMapper;
        this.orderInfoMapper = orderInfoMapper;
        this.orderItemMapper = orderItemMapper;
        this.dishMapper = dishMapper;
        this.stockChangeLogMapper = stockChangeLogMapper;
        this.couponService = couponService;
        this.paymentGateway = paymentGateway;
        this.orderEventPublisher = orderEventPublisher;
    }

    /** 申请退款(仅 PAID/PREPARING/DELIVERING 可申请)。 */
    @Transactional
    public void apply(Long userId, String orderNo, String reason) {
        OrderInfo order = requireOrder(orderNo);
        if (!order.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!OrderStateMachine.canTransit(order.getStatus(), Constants.OrderStatus.REFUNDING)) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        Long existed = refundRecordMapper.selectCount(new LambdaQueryWrapper<RefundRecord>()
                .eq(RefundRecord::getOrderId, order.getId())
                .in(RefundRecord::getStatus, Constants.RefundStatus.PENDING, Constants.RefundStatus.APPROVED));
        if (existed != null && existed > 0) {
            throw new BizException(ResultCode.REFUND_EXISTS);
        }
        RefundRecord record = new RefundRecord();
        record.setOrderId(order.getId());
        record.setOrderNo(orderNo);
        record.setUserId(userId);
        record.setReason(reason);
        record.setAmount(order.getPayAmount());
        record.setStatus(Constants.RefundStatus.PENDING);
        refundRecordMapper.insert(record);
        // 订单 REFUNDING(条件更新)
        int rows = orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, order.getId())
                .eq(OrderInfo::getStatus, order.getStatus())
                .set(OrderInfo::getStatus, Constants.OrderStatus.REFUNDING));
        if (rows == 0) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        orderEventPublisher.publish(orderNo, order.getUserId(), order.getMerchantId(),
                Constants.Mq.RK_REFUND, "退款申请已提交，等待商家处理",
                Constants.NotificationType.PAYMENT, "REFUND_APPLIED");
    }

    /** 商家退款列表。 */
    public PageResult<RefundVO> pageByMerchant(Long merchantId, PageQuery pq) {
        Page<RefundRecord> page = new Page<>(pq.getPage(), pq.getSize());
        // 通过订单归属过滤该商家的退款单
        List<Long> orderIds = orderInfoMapper.selectList(new LambdaQueryWrapper<OrderInfo>()
                        .eq(OrderInfo::getMerchantId, merchantId))
                .stream().map(OrderInfo::getId).collect(Collectors.toList());
        if (orderIds.isEmpty()) {
            return PageResult.of(List.of(), 0, pq.getSize(), pq.getPage());
        }
        Page<RefundRecord> result = refundRecordMapper.selectPage(page,
                new LambdaQueryWrapper<RefundRecord>()
                        .in(RefundRecord::getOrderId, orderIds)
                        .orderByDesc(RefundRecord::getId));
        List<RefundVO> vos = result.getRecords().stream().map(RefundService::toVO).collect(Collectors.toList());
        return PageResult.of(vos, result.getTotal(), pq.getSize(), pq.getPage());
    }

    /** 同意退款: 模拟渠道退款 + 订单 REFUNDED + 库存/券回补。 */
    @Transactional
    public void approve(Long merchantId, Long refundId) {
        RefundRecord record = requireRefund(refundId);
        OrderInfo order = requireOrder(record.getOrderNo());
        if (!order.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!Constants.RefundStatus.PENDING.equals(record.getStatus())) {
            return; // 幂等: 已审核直接返回
        }
        // 模拟渠道退款
        paymentGateway.refund(record.getOrderNo(), record.getAmount());
        // 退款单 PENDING -> APPROVED -> REFUNDED
        int rows = refundRecordMapper.update(null, new LambdaUpdateWrapper<RefundRecord>()
                .eq(RefundRecord::getId, refundId)
                .eq(RefundRecord::getStatus, Constants.RefundStatus.PENDING)
                .set(RefundRecord::getStatus, Constants.RefundStatus.REFUNDED)
                .set(RefundRecord::getReviewerId, merchantId));
        if (rows == 0) {
            return;
        }
        // 订单 REFUNDING -> REFUNDED
        orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, order.getId())
                .eq(OrderInfo::getStatus, Constants.OrderStatus.REFUNDING)
                .set(OrderInfo::getStatus, Constants.OrderStatus.REFUNDED));
        // 回补库存 + 退券
        restoreStock(order.getId());
        couponService.releaseCoupon(order.getUserId(), order.getCouponId(), order.getId());
        orderEventPublisher.publish(order.getOrderNo(), order.getUserId(), order.getMerchantId(),
                Constants.Mq.RK_REFUND, "退款成功，金额已原路退回",
                Constants.NotificationType.PAYMENT, "REFUND_APPROVED");
    }

    /** 拒绝退款: 订单回到 PAID。 */
    @Transactional
    public void reject(Long merchantId, Long refundId, String reason) {
        RefundRecord record = requireRefund(refundId);
        OrderInfo order = requireOrder(record.getOrderNo());
        if (!order.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!Constants.RefundStatus.PENDING.equals(record.getStatus())) {
            return; // 幂等
        }
        int rows = refundRecordMapper.update(null, new LambdaUpdateWrapper<RefundRecord>()
                .eq(RefundRecord::getId, refundId)
                .eq(RefundRecord::getStatus, Constants.RefundStatus.PENDING)
                .set(RefundRecord::getStatus, Constants.RefundStatus.REJECTED)
                .set(RefundRecord::getReviewerId, merchantId)
                .set(RefundRecord::getRejectReason, reason));
        if (rows == 0) {
            return;
        }
        orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, order.getId())
                .eq(OrderInfo::getStatus, Constants.OrderStatus.REFUNDING)
                .set(OrderInfo::getStatus, Constants.OrderStatus.PAID));
        orderEventPublisher.publish(order.getOrderNo(), order.getUserId(), order.getMerchantId(),
                Constants.Mq.RK_REFUND, "退款申请被拒绝" + (StringUtils.hasText(reason) ? "：" + reason : ""),
                Constants.NotificationType.PAYMENT, "REFUND_REJECTED");
    }

    // ---------- 内部 ----------

    private void restoreStock(Long orderId) {
        List<OrderItem> items = orderItemMapper.selectList(new LambdaQueryWrapper<OrderItem>()
                .eq(OrderItem::getOrderId, orderId));
        for (OrderItem it : items) {
            dishMapper.rollbackStock(it.getDishId(), it.getQuantity());
            StockChangeLog log = new StockChangeLog();
            log.setDishId(it.getDishId());
            log.setOrderId(orderId);
            log.setChangeType(Constants.StockChangeType.ROLLBACK);
            log.setChangeQty(it.getQuantity());
            stockChangeLogMapper.insert(log);
        }
    }

    private RefundRecord requireRefund(Long refundId) {
        RefundRecord record = refundRecordMapper.selectById(refundId);
        if (record == null) {
            throw new BizException(ResultCode.REFUND_EXISTS);
        }
        return record;
    }

    private OrderInfo requireOrder(String orderNo) {
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        if (order == null) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        return order;
    }

    public static RefundVO toVO(RefundRecord r) {
        RefundVO vo = new RefundVO();
        vo.setId(r.getId());
        vo.setOrderNo(r.getOrderNo());
        vo.setReason(r.getReason());
        vo.setAmount(r.getAmount());
        vo.setStatus(r.getStatus());
        vo.setRejectReason(r.getRejectReason());
        vo.setCreatedAt(r.getCreatedAt());
        return vo;
    }
}
