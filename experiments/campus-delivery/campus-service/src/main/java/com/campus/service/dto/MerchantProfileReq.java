package com.campus.service.dto;

import jakarta.validation.constraints.Size;
import lombok.Data;

/** 商家资料更新请求。 */
@Data
public class MerchantProfileReq {

    @Size(max = 100)
    private String name;

    @Size(max = 255)
    private String logo;

    @Size(max = 500)
    private String description;

    @Size(max = 50)
    private String category;

    @Size(max = 50)
    private String campusZone;

    private java.math.BigDecimal deliveryFee;

    private java.math.BigDecimal minOrderAmount;

    @Size(max = 20)
    private String openTime;

    @Size(max = 20)
    private String closeTime;
}
