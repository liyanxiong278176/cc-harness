package com.campus.service.vo;

import lombok.Data;

import java.time.LocalDateTime;

/** 通知出参。 */
@Data
public class NotificationVO {

    private Long id;
    private String type;
    private String title;
    private String content;
    private String bizType;
    private String bizId;
    private Integer isRead;
    private LocalDateTime createdAt;
}
