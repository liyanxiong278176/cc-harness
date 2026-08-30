package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

import java.time.LocalDateTime;

/**
 * 订单评价(review)。
 */
@TableName("review")
public class Review extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long orderId;
    private Long userId;
    private Long merchantId;
    private Long dishId;
    /** 1-5 */
    private Integer rating;
    private String content;
    private String images;
    private String reply;
    private LocalDateTime merchantRepliedAt;

    public Long getOrderId() {
        return orderId;
    }

    public void setOrderId(Long orderId) {
        this.orderId = orderId;
    }

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

    public Integer getRating() {
        return rating;
    }

    public void setRating(Integer rating) {
        this.rating = rating;
    }

    public String getContent() {
        return content;
    }

    public void setContent(String content) {
        this.content = content;
    }

    public String getImages() {
        return images;
    }

    public void setImages(String images) {
        this.images = images;
    }

    public String getReply() {
        return reply;
    }

    public void setReply(String reply) {
        this.reply = reply;
    }

    public LocalDateTime getMerchantRepliedAt() {
        return merchantRepliedAt;
    }

    public void setMerchantRepliedAt(LocalDateTime merchantRepliedAt) {
        this.merchantRepliedAt = merchantRepliedAt;
    }
}
