package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 收货地址请求。 */
@Data
public class AddressReq {

    @NotBlank(message = "收货人不能为空")
    @Size(max = 50)
    private String receiverName;

    @NotBlank(message = "收货手机号不能为空")
    private String receiverPhone;

    @NotBlank(message = "校区不能为空")
    @Size(max = 50)
    private String campusZone;

    @NotBlank(message = "详细地址不能为空")
    @Size(max = 200)
    private String detail;

    private Integer isDefault;
}
