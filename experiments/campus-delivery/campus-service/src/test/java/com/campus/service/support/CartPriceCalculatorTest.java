package com.campus.service.support;

import com.campus.service.support.CartPriceCalculator.Line;
import com.campus.service.support.CartPriceCalculator.Result;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class CartPriceCalculatorTest {

    @Test
    void goodsAmountSumsPriceTimesQty() {
        List<Line> lines = List.of(
                new Line(new BigDecimal("12.50"), 2),
                new Line(new BigDecimal("3.00"), 1));
        assertEquals(new BigDecimal("28.00"), CartPriceCalculator.goodsAmount(lines));
    }

    @Test
    void payAmountWithFeeAndDiscount() {
        List<Line> lines = List.of(new Line(new BigDecimal("10.00"), 2));
        Result r = CartPriceCalculator.calculate(lines, new BigDecimal("2.00"), new BigDecimal("5.00"));
        assertEquals(new BigDecimal("20.00"), r.getGoodsAmount());
        assertEquals(new BigDecimal("2.00"), r.getDeliveryFee());
        assertEquals(new BigDecimal("5.00"), r.getDiscountAmount());
        assertEquals(new BigDecimal("17.00"), r.getPayAmount());
    }

    @Test
    void discountCappedAtGoodsAmount() {
        List<Line> lines = List.of(new Line(new BigDecimal("5.00"), 1));
        Result r = CartPriceCalculator.calculate(lines, BigDecimal.ZERO, new BigDecimal("99.00"));
        assertEquals(new BigDecimal("5.00"), r.getDiscountAmount()); // 截断
        assertEquals(new BigDecimal("0.00"), r.getPayAmount());
    }

    @Test
    void nullFeeAndDiscountTreatedAsZero() {
        List<Line> lines = List.of(new Line(new BigDecimal("7.77"), 3));
        Result r = CartPriceCalculator.calculate(lines, null, null);
        assertEquals(new BigDecimal("23.31"), r.getGoodsAmount());
        assertEquals(new BigDecimal("0.00"), r.getDeliveryFee());
        assertEquals(new BigDecimal("23.31"), r.getPayAmount());
    }

    @Test
    void perLineRoundingIsHalfUp() {
        // 单价 0.105 * 3 -> 每行 0.32(0.105*1 保留 2 位为 0.10,合计 0.30)
        List<Line> lines = List.of(
                new Line(new BigDecimal("0.105"), 1),
                new Line(new BigDecimal("0.105"), 1),
                new Line(new BigDecimal("0.105"), 1));
        assertEquals(new BigDecimal("0.30"), CartPriceCalculator.goodsAmount(lines));
    }
}
