package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

import java.time.LocalDateTime;

/**
 * 用户优惠券(user_coupon)。
 * 核销使用条件更新(UNUSED->USED),见 {@code UserCouponMapper.markUsed}。
 */
@TableName("user_coupon")
public class UserCoupon extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long userId;
    private Long couponId;
    /** UNUSED/USED/EXPIRED */
    private String status;
    /** 核销时的订单ID */
    private Long usedOrderId;
    private LocalDateTime receivedAt;
    private LocalDateTime usedAt;
    private LocalDateTime expireAt;

    public Long getUserId() {
        return userId;
    }

    public void setUserId(Long userId) {
        this.userId = userId;
    }

    public Long getCouponId() {
        return couponId;
    }

    public void setCouponId(Long couponId) {
        this.couponId = couponId;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public Long getUsedOrderId() {
        return usedOrderId;
    }

    public void setUsedOrderId(Long usedOrderId) {
        this.usedOrderId = usedOrderId;
    }

    public LocalDateTime getReceivedAt() {
        return receivedAt;
    }

    public void setReceivedAt(LocalDateTime receivedAt) {
        this.receivedAt = receivedAt;
    }

    public LocalDateTime getUsedAt() {
        return usedAt;
    }

    public void setUsedAt(LocalDateTime usedAt) {
        this.usedAt = usedAt;
    }

    public LocalDateTime getExpireAt() {
        return expireAt;
    }

    public void setExpireAt(LocalDateTime expireAt) {
        this.expireAt = expireAt;
    }
}
