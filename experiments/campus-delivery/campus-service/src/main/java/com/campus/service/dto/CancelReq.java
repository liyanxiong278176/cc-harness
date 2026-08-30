package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 取消订单请求。 */
@Data
public class CancelReq {

    @NotBlank(message = "取消原因不能为空")
    @Size(max = 200)
    private String reason;
}
