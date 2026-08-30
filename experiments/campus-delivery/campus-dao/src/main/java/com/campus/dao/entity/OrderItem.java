package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

import java.io.Serializable;
import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 订单明细(快照)(order_item)。
 * 注意: 该表无 deleted/version/created_by/updated_by 列,不继承 {@link BaseEntity}。
 */
@TableName("order_item")
public class OrderItem implements Serializable {

    private static final long serialVersionUID = 1L;

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long orderId;
    private Long dishId;
    private String dishNameSnapshot;
    private BigDecimal dishPriceSnapshot;
    private Integer quantity;
    private BigDecimal subtotal;
    private LocalDateTime createdAt;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public Long getOrderId() {
        return orderId;
    }

    public void setOrderId(Long orderId) {
        this.orderId = orderId;
    }

    public Long getDishId() {
        return dishId;
    }

    public void setDishId(Long dishId) {
        this.dishId = dishId;
    }

    public String getDishNameSnapshot() {
        return dishNameSnapshot;
    }

    public void setDishNameSnapshot(String dishNameSnapshot) {
        this.dishNameSnapshot = dishNameSnapshot;
    }

    public BigDecimal getDishPriceSnapshot() {
        return dishPriceSnapshot;
    }

    public void setDishPriceSnapshot(BigDecimal dishPriceSnapshot) {
        this.dishPriceSnapshot = dishPriceSnapshot;
    }

    public Integer getQuantity() {
        return quantity;
    }

    public void setQuantity(Integer quantity) {
        this.quantity = quantity;
    }

    public BigDecimal getSubtotal() {
        return subtotal;
    }

    public void setSubtotal(BigDecimal subtotal) {
        this.subtotal = subtotal;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }
}
