package com.campus.service.mq;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 订单事件消息体(发送到 MQ,由通知消费者转为站内通知)。
 */
@Data
public class OrderEventMessage {

    private String msgId;
    private String orderNo;
    private Long userId;
    private Long merchantId;
    private String notifyType;
    private String bizType;
    private String bizId;
    private String title;
    private String content;
    private LocalDateTime eventTime;
}
