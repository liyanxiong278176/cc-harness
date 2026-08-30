package com.campus.service.dto;

import jakarta.validation.constraints.Size;
import lombok.Data;

/** 退款审核请求(拒绝时必填原因)。 */
@Data
public class RefundReviewReq {

    @Size(max = 200)
    private String reason;
}
