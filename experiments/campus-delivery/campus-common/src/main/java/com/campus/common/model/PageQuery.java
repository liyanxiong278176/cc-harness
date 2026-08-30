package com.campus.common.model;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import lombok.Data;

/**
 * 分页查询基类。page>=1, size 1..100。
 */
@Data
public class PageQuery {

    @Min(value = 1, message = "page 最小为 1")
    private long page = 1;

    @Min(value = 1, message = "size 最小为 1")
    @Max(value = 100, message = "size 最大为 100")
    private long size = 10;
}
