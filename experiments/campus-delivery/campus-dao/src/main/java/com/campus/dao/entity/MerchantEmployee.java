package com.campus.dao.entity;

import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 商家员工(绑定商家与账号)(merchant_employee)。
 */
@TableName("merchant_employee")
public class MerchantEmployee extends BaseEntity {

    private static final long serialVersionUID = 1L;

    private Long merchantId;
    private Long userId;
    /** OWNER/STAFF */
    private String role;
    /** 1启用 0停用 */
    private Integer status;

    public Long getMerchantId() {
        return merchantId;
    }

    public void setMerchantId(Long merchantId) {
        this.merchantId = merchantId;
    }

    public Long getUserId() {
        return userId;
    }

    public void setUserId(Long userId) {
        this.userId = userId;
    }

    public String getRole() {
        return role;
    }

    public void setRole(String role) {
        this.role = role;
    }

    public Integer getStatus() {
        return status;
    }

    public void setStatus(Integer status) {
        this.status = status;
    }
}
