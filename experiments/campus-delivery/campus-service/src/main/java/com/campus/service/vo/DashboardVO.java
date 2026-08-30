package com.campus.service.vo;

import lombok.Data;

import java.math.BigDecimal;

/** 商家工作台出参。 */
@Data
public class DashboardVO {

    private Long todayOrderCount;
    private BigDecimal todayAmount;
    private Long pendingAcceptCount;
    private Long pendingRefundCount;
    private Long totalDishCount;
    private BigDecimal monthAmount;
}
