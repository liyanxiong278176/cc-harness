package com.campus.service.vo;

import com.campus.common.sensitive.Sensitive;
import com.campus.common.sensitive.SensitiveType;
import lombok.Data;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

/** 订单详情出参。 */
@Data
public class OrderDetailVO {

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
    private String cancelReason;
    private LocalDateTime createdAt;
    private LocalDateTime payTime;
    private String payChannel;
    private String payTradeNo;
    private Long riderId;

    /** 地址快照(脱敏) */
    @Sensitive(SensitiveType.NAME)
    private String receiverName;
    @Sensitive(SensitiveType.PHONE)
    private String receiverPhone;
    private String receiverAddress;

    private List<Item> items;
    /** 是否已评价 */
    private Boolean reviewed;

    @Data
    public static class Item {
        private String dishName;
        private BigDecimal price;
        private Integer quantity;
        private BigDecimal subtotal;
    }
}
