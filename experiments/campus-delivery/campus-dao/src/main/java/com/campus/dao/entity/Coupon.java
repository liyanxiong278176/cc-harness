package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 优惠券模板(coupon)。
 * 领取量使用条件更新防超发,见 {@code CouponMapper.incrementIssued}。
 */
@TableName("coupon")
public class Coupon extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private String name;
    /** FULL_REDUCTION满减 / DISCOUNT折扣 */
    private String type;
    /** 满额门槛 */
    private BigDecimal thresholdAmount;
    /** 满减金额(type=FULL_REDUCTION) */
    private BigDecimal discountAmount;
    /** 折扣率(type=DISCOUNT) */
    private BigDecimal discountRate;
    /** 发行总量 */
    private Integer totalCount;
    /** 已发行量 */
    private Integer issuedCount;
    private LocalDateTime startTime;
    private LocalDateTime endTime;
    /** 1启用 0停用 */
    private Integer status;

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public String getType() {
        return type;
    }

    public void setType(String type) {
        this.type = type;
    }

    public BigDecimal getThresholdAmount() {
        return thresholdAmount;
    }

    public void setThresholdAmount(BigDecimal thresholdAmount) {
        this.thresholdAmount = thresholdAmount;
    }

    public BigDecimal getDiscountAmount() {
        return discountAmount;
    }

    public void setDiscountAmount(BigDecimal discountAmount) {
        this.discountAmount = discountAmount;
    }

    public BigDecimal getDiscountRate() {
        return discountRate;
    }

    public void setDiscountRate(BigDecimal discountRate) {
        this.discountRate = discountRate;
    }

    public Integer getTotalCount() {
        return totalCount;
    }

    public void setTotalCount(Integer totalCount) {
        this.totalCount = totalCount;
    }

    public Integer getIssuedCount() {
        return issuedCount;
    }

    public void setIssuedCount(Integer issuedCount) {
        this.issuedCount = issuedCount;
    }

    public LocalDateTime getStartTime() {
        return startTime;
    }

    public void setStartTime(LocalDateTime startTime) {
        this.startTime = startTime;
    }

    public LocalDateTime getEndTime() {
        return endTime;
    }

    public void setEndTime(LocalDateTime endTime) {
        this.endTime = endTime;
    }

    public Integer getStatus() {
        return status;
    }

    public void setStatus(Integer status) {
        this.status = status;
    }
}
