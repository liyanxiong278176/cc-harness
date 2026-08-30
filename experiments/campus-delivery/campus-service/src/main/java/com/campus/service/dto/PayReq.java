package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Data;

/** 发起支付请求。 */
@Data
public class PayReq {

    @NotBlank(message = "渠道不能为空")
    private String channel;
}
