package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 购物车(cart)。
 */
@TableName("cart")
public class Cart extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long userId;
    private Long merchantId;
    private Long dishId;
    private Integer quantity;
    /** 勾选 1是 0否 */
    private Integer checked;

    public Long getUserId() {
        return userId;
    }

    public void setUserId(Long userId) {
        this.userId = userId;
    }

    public Long getMerchantId() {
        return merchantId;
    }

    public void setMerchantId(Long merchantId) {
        this.merchantId = merchantId;
    }

    public Long getDishId() {
        return dishId;
    }

    public void setDishId(Long dishId) {
        this.dishId = dishId;
    }

    public Integer getQuantity() {
        return quantity;
    }

    public void setQuantity(Integer quantity) {
        this.quantity = quantity;
    }

    public Integer getChecked() {
        return checked;
    }

    public void setChecked(Integer checked) {
        this.checked = checked;
    }
}
