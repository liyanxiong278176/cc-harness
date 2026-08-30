package com.campus.service.dto;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

/** 营业状态请求。 */
@Data
public class BusinessStatusReq {

    @NotNull(message = "isOpen 不能为空")
    private Integer isOpen;
}
