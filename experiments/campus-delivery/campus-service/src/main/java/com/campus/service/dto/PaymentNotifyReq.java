package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Data;

/** 模拟支付回调请求。 */
@Data
public class PaymentNotifyReq {

    @NotBlank(message = "订单号不能为空")
    private String orderNo;

    /** true=支付成功,false=支付失败 */
    private Boolean success;

    private String channel;
}
