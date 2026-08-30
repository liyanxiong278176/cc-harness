package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/** 订单列表项出参。 */
@Data
public class OrderVO {

    private Long id;
    private String orderNo;
    private Long merchantId;
    private String merchantName;
    private String status;
    private BigDecimal totalAmount;
    private BigDecimal discountAmount;
    private BigDecimal deliveryFee;
    private BigDecimal payAmount;
    private String remark;
    private LocalDateTime createdAt;
    private Integer itemCount;
}
