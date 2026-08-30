package com.campus.service.vo;

import lombok.Data;

import java.time.LocalDateTime;

/** 配送任务出参。 */
@Data
public class TaskVO {

    private Long id;
    private String orderNo;
    private Long merchantId;
    private String merchantName;
    private String pickupAddress;
    private String deliveryAddress;
    private String status;
    private Long riderId;
    private LocalDateTime createdAt;
}
