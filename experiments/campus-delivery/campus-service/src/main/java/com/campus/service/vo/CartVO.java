package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;
import java.util.List;

/** 购物车(按商家分组)。 */
@Data
public class CartVO {

    private List<CartGroup> groups;
    private BigDecimal totalAmount;
    private Integer totalCheckedCount;

    @Data
    public static class CartGroup {
        private Long merchantId;
        private String merchantName;
        private Integer isOpen;
        private List<CartItemVO> items;
        private BigDecimal goodsAmount;
    }
}
