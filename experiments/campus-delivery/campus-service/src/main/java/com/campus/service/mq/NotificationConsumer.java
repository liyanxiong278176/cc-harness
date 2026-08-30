package com.campus.service.mq;

import com.campus.common.util.JsonUtils;
import com.campus.service.NotificationService;
import com.rabbitmq.client.Channel;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.support.AmqpHeaders;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.messaging.handler.annotation.Header;
import org.springframework.stereotype.Component;

/**
 * 订单/支付/配送/系统事件消费者: 将事件消息转为站内通知。
 *
 * <p>幂等: 通知表 (user_id,biz_type,biz_id) 唯一键 + NotificationService 先查后插。
 * <p>失败: 手动确认模式下 basicReject(requeue=false),消息进入死信队列(DLQ),
 * 由运维/补偿脚本处理;应用配置 acknowledge-mode=manual、default-requeue-rejected=false。
 */
@Component
@ConditionalOnProperty(name = "app.mq.enabled", havingValue = "true", matchIfMissing = true)
public class NotificationConsumer {

    private static final Logger log = LoggerFactory.getLogger(NotificationConsumer.class);

    private final NotificationService notificationService;

    public NotificationConsumer(NotificationService notificationService) {
        this.notificationService = notificationService;
    }

    @RabbitListener(queues = "#{rabbitConfig.queueNotifyOrder.name}")
    public void onOrderEvent(String payload, Channel channel,
                             @Header(AmqpHeaders.DELIVERY_TAG) long tag) {
        handle(payload, channel, tag);
    }

    @RabbitListener(queues = "#{rabbitConfig.queueNotifyPayment.name}")
    public void onPaymentEvent(String payload, Channel channel,
                               @Header(AmqpHeaders.DELIVERY_TAG) long tag) {
        handle(payload, channel, tag);
    }

    @RabbitListener(queues = "#{rabbitConfig.queueNotifyDelivery.name}")
    public void onDeliveryEvent(String payload, Channel channel,
                                @Header(AmqpHeaders.DELIVERY_TAG) long tag) {
        handle(payload, channel, tag);
    }

    @RabbitListener(queues = "#{rabbitConfig.queueNotifySystem.name}")
    public void onSystemEvent(String payload, Channel channel,
                              @Header(AmqpHeaders.DELIVERY_TAG) long tag) {
        handle(payload, channel, tag);
    }

    private void handle(String payload, Channel channel, long tag) {
        try {
            OrderEventMessage msg = JsonUtils.parse(payload, OrderEventMessage.class);
            if (msg == null || msg.getUserId() == null || msg.getBizType() == null) {
                log.warn("[notify-consumer] ignore malformed payload: {}", truncate(payload));
                channel.basicAck(tag, false);
                return;
            }
            notificationService.create(msg.getUserId(), msg.getNotifyType(),
                    msg.getTitle(), msg.getContent(), msg.getBizType(), msg.getBizId());
            channel.basicAck(tag, false);
            log.debug("[notify-consumer] acked orderNo={} bizType={}", msg.getOrderNo(), msg.getBizType());
        } catch (Exception e) {
            log.error("[notify-consumer] consume failed, reject to DLQ: {}", truncate(payload), e);
            try {
                channel.basicReject(tag, false);
            } catch (Exception rejectEx) {
                log.error("[notify-consumer] basicReject failed", rejectEx);
            }
        }
    }

    private static String truncate(String s) {
        if (s == null) {
            return "";
        }
        return s.length() > 300 ? s.substring(0, 300) : s;
    }
}
