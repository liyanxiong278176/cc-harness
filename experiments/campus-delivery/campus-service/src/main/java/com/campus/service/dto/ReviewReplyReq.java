package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 商家回复评价请求。 */
@Data
public class ReviewReplyReq {

    @NotBlank(message = "回复内容不能为空")
    @Size(max = 500)
    private String reply;
}
