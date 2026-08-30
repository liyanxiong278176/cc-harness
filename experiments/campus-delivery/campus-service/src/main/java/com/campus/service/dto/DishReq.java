package com.campus.service.dto;

import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import lombok.Data;

import java.math.BigDecimal;

/** 菜品新增/更新请求。 */
@Data
public class DishReq {

    @NotNull(message = "分类ID不能为空")
    private Long categoryId;

    @NotBlank(message = "菜品名不能为空")
    @Size(max = 100)
    private String name;

    @Size(max = 500)
    private String description;

    @Size(max = 255)
    private String image;

    @NotNull(message = "价格不能为空")
    @DecimalMin(value = "0.01", message = "价格必须大于 0")
    private BigDecimal price;

    private BigDecimal originalPrice;

    @Min(value = 0, message = "库存不能为负")
    private Integer stock;
}
