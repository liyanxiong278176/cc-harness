package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;

/** 购物车行出参。 */
@Data
public class CartItemVO {

    private Long dishId;
    private String dishName;
    private String image;
    private BigDecimal price;
    private Integer quantity;
    private Integer checked;
    /** 当前库存上限(前端限制可加购数量) */
    private Integer stock;
}
