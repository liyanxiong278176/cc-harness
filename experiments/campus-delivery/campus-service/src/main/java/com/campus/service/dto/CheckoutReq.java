package com.campus.service.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 结算下单请求。 */
@Data
public class CheckoutReq {

    @NotNull(message = "商家ID不能为空")
    private Long merchantId;

    @NotNull(message = "收货地址ID不能为空")
    private Long addressId;

    /** 优惠券ID(可为空) */
    private Long couponId;

    @Size(max = 200)
    private String remark;
}
