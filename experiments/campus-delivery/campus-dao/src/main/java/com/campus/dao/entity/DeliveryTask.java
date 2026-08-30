package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

import java.time.LocalDateTime;

/**
 * 配送任务(delivery_task)。
 */
@TableName("delivery_task")
public class DeliveryTask extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long orderId;
    private String orderNo;
    private Long riderId;
    private Long merchantId;
    private Long userId;
    private String pickupAddress;
    private String deliveryAddress;
    /** WAIT_ACCEPT/ACCEPTED/PICKED/DELIVERING/DELIVERED/CANCELLED */
    private String status;
    private LocalDateTime acceptTime;
    private LocalDateTime pickedTime;
    private LocalDateTime deliveredTime;

    public Long getOrderId() {
        return orderId;
    }

    public void setOrderId(Long orderId) {
        this.orderId = orderId;
    }

    public String getOrderNo() {
        return orderNo;
    }

    public void setOrderNo(String orderNo) {
        this.orderNo = orderNo;
    }

    public Long getRiderId() {
        return riderId;
    }

    public void setRiderId(Long riderId) {
        this.riderId = riderId;
    }

    public Long getMerchantId() {
        return merchantId;
    }

    public void setMerchantId(Long merchantId) {
        this.merchantId = merchantId;
    }

    public Long getUserId() {
        return userId;
    }

    public void setUserId(Long userId) {
        this.userId = userId;
    }

    public String getPickupAddress() {
        return pickupAddress;
    }

    public void setPickupAddress(String pickupAddress) {
        this.pickupAddress = pickupAddress;
    }

    public String getDeliveryAddress() {
        return deliveryAddress;
    }

    public void setDeliveryAddress(String deliveryAddress) {
        this.deliveryAddress = deliveryAddress;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public LocalDateTime getAcceptTime() {
        return acceptTime;
    }

    public void setAcceptTime(LocalDateTime acceptTime) {
        this.acceptTime = acceptTime;
    }

    public LocalDateTime getPickedTime() {
        return pickedTime;
    }

    public void setPickedTime(LocalDateTime pickedTime) {
        this.pickedTime = pickedTime;
    }

    public LocalDateTime getDeliveredTime() {
        return deliveredTime;
    }

    public void setDeliveredTime(LocalDateTime deliveredTime) {
        this.deliveredTime = deliveredTime;
    }
}
