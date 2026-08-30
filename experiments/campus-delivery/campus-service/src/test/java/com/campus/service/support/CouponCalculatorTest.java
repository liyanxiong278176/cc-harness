package com.campus.service.support;

import com.campus.common.constant.Constants;
import com.campus.dao.entity.Coupon;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.LocalDateTime;

import static org.junit.jupiter.api.Assertions.assertEquals;

class CouponCalculatorTest {

    private Coupon coupon(String type, String threshold, String discount, String start, String end) {
        Coupon c = new Coupon();
        c.setType(type);
        c.setThresholdAmount(new BigDecimal(threshold));
        c.setDiscountAmount(discount == null ? null : new BigDecimal(discount));
        c.setDiscountRate(discount == null ? null : new BigDecimal(discount));
        c.setStartTime(LocalDateTime.parse(start));
        c.setEndTime(LocalDateTime.parse(end));
        return c;
    }

    @Test
    void fullReductionUnderThresholdNoDiscount() {
        Coupon c = coupon(Constants.CouponType.FULL_REDUCTION, "30.00", "5.00",
                "2024-01-01T00:00:00", "2030-01-01T00:00:00");
        assertEquals(new BigDecimal("0.00"),
                CouponCalculator.discountAmount(c, new BigDecimal("29.99"), LocalDateTime.now()));
    }

    @Test
    void fullReductionOverThreshold() {
        Coupon c = coupon(Constants.CouponType.FULL_REDUCTION, "30.00", "5.00",
                "2024-01-01T00:00:00", "2030-01-01T00:00:00");
        assertEquals(new BigDecimal("5.00"),
                CouponCalculator.discountAmount(c, new BigDecimal("30.00"), LocalDateTime.now()));
    }

    @Test
    void discountCouponTenPercentOff() {
        Coupon c = coupon(Constants.CouponType.DISCOUNT, "0.00", "0.900",
                "2024-01-01T00:00:00", "2030-01-01T00:00:00");
        assertEquals(new BigDecimal("10.00"),
                CouponCalculator.discountAmount(c, new BigDecimal("100.00"), LocalDateTime.now()));
    }

    @Test
    void expiredCouponNoDiscount() {
        Coupon c = coupon(Constants.CouponType.FULL_REDUCTION, "0.00", "10.00",
                "2020-01-01T00:00:00", "2021-01-01T00:00:00");
        assertEquals(new BigDecimal("0.00"),
                CouponCalculator.discountAmount(c, new BigDecimal("100.00"), LocalDateTime.now()));
    }
}
