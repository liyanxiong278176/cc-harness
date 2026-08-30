package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 菜品分类(dish_category)。
 */
@TableName("dish_category")
public class DishCategory extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long merchantId;
    private String name;
    /** 排序(升序) */
    private Integer sortOrder;
    /** 1启用 0停用 */
    private Integer status;

    public Long getMerchantId() {
        return merchantId;
    }

    public void setMerchantId(Long merchantId) {
        this.merchantId = merchantId;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public Integer getSortOrder() {
        return sortOrder;
    }

    public void setSortOrder(Integer sortOrder) {
        this.sortOrder = sortOrder;
    }

    public Integer getStatus() {
        return status;
    }

    public void setStatus(Integer status) {
        this.status = status;
    }
}
