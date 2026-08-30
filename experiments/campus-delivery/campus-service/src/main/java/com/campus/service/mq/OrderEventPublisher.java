package com.campus.service.mq;

import com.campus.common.constant.Constants;
import com.campus.common.util.IdUtils;
import com.campus.common.util.JsonUtils;
import com.campus.dao.entity.MqMessage;
import com.campus.dao.mapper.MqMessageMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;

/**
 * 订单事件发布器(可靠投递 Outbox 模式)。
 * 在业务事务内仅写 mq_message(PENDING),由 MqRelayJob 定时投递,
 * 避免本地事务与 MQ 发送的一致性问题(事务消息)。
 */
@Service
public class OrderEventPublisher {

    private static final Logger log = LoggerFactory.getLogger(OrderEventPublisher.class);

    private final MqMessageMapper mqMessageMapper;

    public OrderEventPublisher(MqMessageMapper mqMessageMapper) {
        this.mqMessageMapper = mqMessageMapper;
    }

    @Transactional
    public void publish(String orderNo, Long userId, Long merchantId,
                        String routingKey, String title, String notifyType, String bizType) {
        String msgId = IdUtils.msgId();
        OrderEventMessage msg = new OrderEventMessage();
        msg.setMsgId(msgId);
        msg.setOrderNo(orderNo);
        msg.setUserId(userId);
        msg.setMerchantId(merchantId);
        msg.setNotifyType(notifyType);
        msg.setBizType(bizType);
        msg.setBizId(orderNo);
        msg.setTitle(title);
        msg.setContent(title + "(订单 " + orderNo + ")");
        msg.setEventTime(LocalDateTime.now());

        MqMessage row = new MqMessage();
        row.setMsgId(msgId);
        row.setExchange(Constants.Mq.EXCHANGE_NOTIFY);
        row.setRoutingKey(routingKey);
        row.setPayload(JsonUtils.toJson(msg));
        row.setStatus(Constants.MqMsgStatus.PENDING);
        row.setRetryCount(0);
        row.setNextRetryTime(LocalDateTime.now());
        mqMessageMapper.insert(row);
        log.debug("[outbox] msgId={} orderNo={} rk={}", msgId, orderNo, routingKey);
    }
}
