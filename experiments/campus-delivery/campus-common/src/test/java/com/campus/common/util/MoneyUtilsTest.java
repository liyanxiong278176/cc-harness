package com.campus.common.util;

import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MoneyUtilsTest {

    @Test
    void ofParsesAndScales() {
        assertEquals(new BigDecimal("12.35"), MoneyUtils.of("12.345"));
        assertEquals(new BigDecimal("12.34"), MoneyUtils.of("12.344"));
        assertEquals(new BigDecimal("0.00"), MoneyUtils.of((BigDecimal) null));
    }

    @Test
    void arithmeticKeepsScale2() {
        assertEquals(new BigDecimal("15.25"), MoneyUtils.add(new BigDecimal("10.005"), new BigDecimal("5.245")));
        assertEquals(new BigDecimal("5.00"), MoneyUtils.subtract(new BigDecimal("10.00"), new BigDecimal("5.00")));
        assertEquals(new BigDecimal("12.35"), MoneyUtils.multiply(new BigDecimal("12.345"), BigDecimal.ONE));
    }

    @Test
    void compareAndGe() {
        assertEquals(0, MoneyUtils.compare(new BigDecimal("1.001"), new BigDecimal("1.00")));
        assertTrue(MoneyUtils.ge(new BigDecimal("2.00"), new BigDecimal("1.99")));
    }

    @Test
    void discountIsHalfUp() {
        assertEquals(new BigDecimal("9.00"), MoneyUtils.discounted(new BigDecimal("10.00"), new BigDecimal("0.900")));
        assertEquals(new BigDecimal("1.50"), MoneyUtils.discounted(new BigDecimal("10.00"), new BigDecimal("0.15")));
    }
}
