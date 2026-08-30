package com.campus.service.dto;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

/** 菜品上下架请求。 */
@Data
public class DishStatusReq {

    @NotNull(message = "status 不能为空(1 上架 / 0 下架)")
    private Integer status;
}
