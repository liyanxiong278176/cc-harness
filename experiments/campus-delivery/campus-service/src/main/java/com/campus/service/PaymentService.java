package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.entity.PaymentRecord;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.PaymentRecordMapper;
import com.campus.service.adapter.PaymentGateway;
import com.campus.service.dto.PaymentNotifyReq;
import com.campus.service.mq.OrderEventPublisher;
import com.campus.service.support.CacheClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.Map;

/**
 * 支付服务: 创建支付单 + 模拟回调入账(幂等)。
 * 幂等实现: 回调先按 (order_no, status=CREATED) 条件更新,影响行数=0 视为重复回调;
 * 外加 Redis 去重键兜底,支付流水 trade_no 唯一键兜底。
 */
@Service
public class PaymentService {

    private static final Logger log = LoggerFactory.getLogger(PaymentService.class);

    private final PaymentRecordMapper paymentRecordMapper;
    private final OrderInfoMapper orderInfoMapper;
    private final PaymentGateway paymentGateway;
    private final CacheClient cacheClient;
    private final OrderEventPublisher orderEventPublisher;

    public PaymentService(PaymentRecordMapper paymentRecordMapper,
                          OrderInfoMapper orderInfoMapper,
                          PaymentGateway paymentGateway,
                          CacheClient cacheClient,
                          OrderEventPublisher orderEventPublisher) {
        this.paymentRecordMapper = paymentRecordMapper;
        this.orderInfoMapper = orderInfoMapper;
        this.paymentGateway = paymentGateway;
        this.cacheClient = cacheClient;
        this.orderEventPublisher = orderEventPublisher;
    }

    /** 发起支付: 返回模拟支付参数。 */
    @Transactional
    public Map<String, String> pay(Long userId, String orderNo, String channel) {
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        if (order == null || !order.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!Constants.OrderStatus.CREATED.equals(order.getStatus())) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        PaymentGateway.PaymentResult result = paymentGateway.createPayment(orderNo, order.getPayAmount(), channel);
        PaymentRecord record = new PaymentRecord();
        record.setOrderId(order.getId());
        record.setOrderNo(orderNo);
        record.setUserId(userId);
        record.setChannel(channel == null ? "MOCK" : channel);
        record.setTradeNo(result.getTradeNo());
        record.setAmount(order.getPayAmount());
        record.setStatus(Constants.PayStatus.PENDING);
        paymentRecordMapper.insert(record);
        Map<String, String> out = new HashMap<>();
        out.put("paymentNo", result.getTradeNo());
        out.put("payParams", com.campus.common.util.JsonUtils.toJson(result.getPayParams()));
        return out;
    }

    /**
     * 模拟支付回调。幂等: 重复回调直接返回成功结果。
     */
    @Transactional
    public Map<String, Object> notify(PaymentNotifyReq req) {
        String orderNo = req.getOrderNo();
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        if (order == null) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        // Redis 去重(回调风暴防护)
        if (!cacheClient.setIfAbsent(Constants.RedisKeys.NOTIFY_DEDUP + orderNo, "1", 60)) {
            log.info("[pay-notify] duplicate callback (redis dedup) orderNo={}", orderNo);
            return result(orderNo, true);
        }
        boolean success = req.getSuccess() == null || req.getSuccess();
        if (!success) {
            paymentRecordMapper.update(null, new LambdaUpdateWrapper<PaymentRecord>()
                    .eq(PaymentRecord::getOrderNo, orderNo)
                    .set(PaymentRecord::getStatus, Constants.PayStatus.FAILED));
            return result(orderNo, false);
        }
        // 支付流水置 SUCCESS(幂等: 已 SUCCESS 则跳过)
        int payRows = paymentRecordMapper.update(null, new LambdaUpdateWrapper<PaymentRecord>()
                .eq(PaymentRecord::getOrderNo, orderNo)
                .eq(PaymentRecord::getStatus, Constants.PayStatus.PENDING)
                .set(PaymentRecord::getStatus, Constants.PayStatus.SUCCESS)
                .set(PaymentRecord::getPaidAt, LocalDateTime.now()));
        if (payRows == 0) {
            Long paid = paymentRecordMapper.selectCount(new LambdaQueryWrapper<PaymentRecord>()
                    .eq(PaymentRecord::getOrderNo, orderNo)
                    .eq(PaymentRecord::getStatus, Constants.PayStatus.SUCCESS));
            if (paid != null && paid > 0) {
                return result(orderNo, true); // 已入账,幂等返回
            }
        }
        // 订单条件更新 CREATED -> PAID(并发回调互斥锚点)
        int orderRows = orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo)
                .eq(OrderInfo::getStatus, Constants.OrderStatus.CREATED)
                .set(OrderInfo::getStatus, Constants.OrderStatus.PAID)
                .set(OrderInfo::getPayTime, LocalDateTime.now())
                .set(OrderInfo::getPayChannel, "MOCK")
                .set(OrderInfo::getPayTradeNo, paymentRecordMapper.selectOne(
                        new LambdaQueryWrapper<PaymentRecord>()
                                .eq(PaymentRecord::getOrderNo, orderNo).last("LIMIT 1")) == null
                        ? "" : paymentRecordMapper.selectOne(new LambdaQueryWrapper<PaymentRecord>()
                        .eq(PaymentRecord::getOrderNo, orderNo).last("LIMIT 1")).getTradeNo()));
        if (orderRows == 0) {
            // 已被其他回调置为 PAID(幂等)
            return result(orderNo, true);
        }
        orderEventPublisher.publish(orderNo, order.getUserId(), order.getMerchantId(),
                Constants.Mq.RK_ORDER_PAID, "订单支付成功",
                Constants.NotificationType.PAYMENT, "ORDER_PAID");
        log.info("[pay-notify] order {} paid", orderNo);
        return result(orderNo, true);
    }

    private Map<String, Object> result(String orderNo, boolean ok) {
        Map<String, Object> r = new HashMap<>();
        r.put("orderNo", orderNo);
        r.put("success", ok);
        return r;
    }
}
