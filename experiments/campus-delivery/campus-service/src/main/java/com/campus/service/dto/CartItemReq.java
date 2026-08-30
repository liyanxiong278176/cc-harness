package com.campus.service.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import lombok.Data;

/** 加购请求。 */
@Data
public class CartItemReq {

    @NotNull(message = "菜品ID不能为空")
    private Long dishId;

    @NotNull(message = "数量不能为空")
    @Min(value = 1, message = "数量至少 1")
    @Max(value = 99, message = "单次最多 99")
    private Integer quantity;
}
