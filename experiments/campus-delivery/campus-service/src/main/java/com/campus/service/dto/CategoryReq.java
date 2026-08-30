package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import lombok.Data;

/** 菜品分类请求。 */
@Data
public class CategoryReq {

    @NotBlank(message = "分类名不能为空")
    @Size(max = 50)
    private String name;

    @NotNull(message = "排序不能为空")
    private Integer sortOrder;
}
