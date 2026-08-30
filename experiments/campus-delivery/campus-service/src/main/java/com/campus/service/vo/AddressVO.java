package com.campus.service.vo;

import com.campus.common.sensitive.Sensitive;
import com.campus.common.sensitive.SensitiveType;
import lombok.Data;

/** 收货地址出参(敏感字段脱敏)。 */
@Data
public class AddressVO {

    private Long id;

    @Sensitive(SensitiveType.NAME)
    private String receiverName;

    @Sensitive(SensitiveType.PHONE)
    private String receiverPhone;

    private String campusZone;
    private String detail;
    private Integer isDefault;
}
