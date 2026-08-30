package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/** 退款单出参。 */
@Data
public class RefundVO {

    private Long id;
    private String orderNo;
    private String reason;
    private BigDecimal amount;
    private String status;
    private String rejectReason;
    private LocalDateTime createdAt;
}
