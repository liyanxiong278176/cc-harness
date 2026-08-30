package com.campus.service.vo;

import lombok.Data;

/** 分类出参。 */
@Data
public class CategoryVO {

    private Long id;
    private String name;
    private Integer sortOrder;
    private Integer status;
}
