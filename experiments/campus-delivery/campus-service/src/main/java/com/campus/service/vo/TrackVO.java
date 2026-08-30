package com.campus.service.vo;

import lombok.Data;

import java.time.LocalDateTime;

/** 订单跟踪出参。 */
@Data
public class TrackVO {

    private String orderNo;
    private String orderStatus;
    private String payStatus;
    private String deliveryStatus;
    private Long riderId;
    private java.math.BigDecimal payAmount;
    private LocalDateTime createdAt;
    private LocalDateTime payTime;
    private LocalDateTime cancelTime;
    private LocalDateTime deliveredTime;
}
