package com.campus.service.vo;

import com.campus.common.sensitive.Sensitive;
import com.campus.common.sensitive.SensitiveType;
import lombok.Data;

import java.time.LocalDateTime;

/** 评价出参。 */
@Data
public class ReviewVO {

    private Long id;
    private Long orderId;
    private String orderNo;
    private Integer rating;
    private String content;
    private String images;
    private String reply;
    private LocalDateTime createdAt;

    @Sensitive(SensitiveType.NAME)
    private String userName;
}
