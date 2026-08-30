package com.campus.service.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 评价创建请求。 */
@Data
public class ReviewCreateReq {

    @NotNull(message = "评分不能为空")
    @Min(value = 1, message = "评分 1-5")
    @Max(value = 5, message = "评分 1-5")
    private Integer rating;

    @Size(max = 1000)
    private String content;

    @Size(max = 1000)
    private String images;
}
