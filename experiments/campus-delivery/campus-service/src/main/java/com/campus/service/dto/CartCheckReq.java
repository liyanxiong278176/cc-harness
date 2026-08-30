package com.campus.service.dto;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

/** 购物车勾选请求。 */
@Data
public class CartCheckReq {

    @NotNull(message = "checked 不能为空")
    private Integer checked;
}
