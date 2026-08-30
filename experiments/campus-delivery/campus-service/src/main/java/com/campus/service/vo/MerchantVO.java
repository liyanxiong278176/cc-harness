package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;

/** 商家出参。 */
@Data
public class MerchantVO {

    private Long id;
    private String name;
    private String logo;
    private String description;
    private String category;
    private String campusZone;
    private BigDecimal deliveryFee;
    private BigDecimal minOrderAmount;
    private String openTime;
    private String closeTime;
    private Integer isOpen;
    private BigDecimal rating;
    private Integer ratingCount;
}
