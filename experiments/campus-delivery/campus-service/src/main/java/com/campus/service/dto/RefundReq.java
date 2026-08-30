package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 退款申请请求。 */
@Data
public class RefundReq {

    @NotBlank(message = "退款原因不能为空")
    @Size(max = 200)
    private String reason;
}
