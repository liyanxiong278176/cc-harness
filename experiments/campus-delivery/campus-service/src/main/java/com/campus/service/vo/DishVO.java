package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;

/** 菜品出参。 */
@Data
public class DishVO {

    private Long id;
    private Long merchantId;
    private Long categoryId;
    private String skuCode;
    private String name;
    private String description;
    private String image;
    private BigDecimal price;
    private BigDecimal originalPrice;
    private Integer stock;
    private Integer soldCount;
    private Integer status;
}
