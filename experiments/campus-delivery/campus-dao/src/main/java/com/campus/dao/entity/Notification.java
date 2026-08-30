package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

import java.time.LocalDateTime;

/**
 * 站内通知(notification)。
 * (user_id,biz_type,biz_id) 唯一键保证消息幂等。
 */
@TableName("notification")
public class Notification extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long userId;
    /** ORDER_STATUS/PAYMENT/DELIVERY/SYSTEM */
    private String type;
    private String title;
    private String content;
    /** 业务类型(幂等键一部分) */
    private String bizType;
    /** 业务ID(幂等键一部分) */
    private String bizId;
    /** 是否已读 1是 0否 */
    private Integer isRead;
    private LocalDateTime readAt;

    public Long getUserId() {
        return userId;
    }

    public void setUserId(Long userId) {
        this.userId = userId;
    }

    public String getType() {
        return type;
    }

    public void setType(String type) {
        this.type = type;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getContent() {
        return content;
    }

    public void setContent(String content) {
        this.content = content;
    }

    public String getBizType() {
        return bizType;
    }

    public void setBizType(String bizType) {
        this.bizType = bizType;
    }

    public String getBizId() {
        return bizId;
    }

    public void setBizId(String bizId) {
        this.bizId = bizId;
    }

    public Integer getIsRead() {
        return isRead;
    }

    public void setIsRead(Integer isRead) {
        this.isRead = isRead;
    }

    public LocalDateTime getReadAt() {
        return readAt;
    }

    public void setReadAt(LocalDateTime readAt) {
        this.readAt = readAt;
    }
}
