package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/** 用户券出参。 */
@Data
public class CouponVO {

    private Long id;
    private String name;
    private String type;
    private BigDecimal thresholdAmount;
    private BigDecimal discountAmount;
    private BigDecimal discountRate;
    /** 用户券状态: UNUSED / USED / EXPIRED */
    private String status;
    private LocalDateTime expireAt;
    private LocalDateTime startTime;
    private LocalDateTime endTime;
}
