package com.campus.common.util;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 金额工具: 一律 BigDecimal,半向上取整 2 位小数。
 */
public final class MoneyUtils {

    private MoneyUtils() {
    }

    public static final int SCALE = 2;

    public static BigDecimal of(String v) {
        return new BigDecimal(v).setScale(SCALE, RoundingMode.HALF_UP);
    }

    public static BigDecimal of(BigDecimal v) {
        return v == null ? BigDecimal.ZERO.setScale(SCALE) : v.setScale(SCALE, RoundingMode.HALF_UP);
    }

    /** null 视为 0(与 {@link #of} 的 null 语义一致,供精确运算前使用)。 */
    private static BigDecimal nz(BigDecimal v) {
        return v == null ? BigDecimal.ZERO : v;
    }

    /** 先精确相加、末尾统一 2 位半向上取整(避免中间舍入误差,如 10.005+5.245=15.25)。 */
    public static BigDecimal add(BigDecimal a, BigDecimal b) {
        return of(nz(a).add(nz(b)));
    }

    /** 先精确相减、末尾统一 2 位半向上取整(同 {@link #add})。 */
    public static BigDecimal subtract(BigDecimal a, BigDecimal b) {
        return of(nz(a).subtract(nz(b)));
    }

    public static BigDecimal multiply(BigDecimal a, BigDecimal b) {
        return of(a).multiply(of(b)).setScale(SCALE, RoundingMode.HALF_UP);
    }

    public static int compare(BigDecimal a, BigDecimal b) {
        return of(a).compareTo(of(b));
    }

    public static boolean ge(BigDecimal a, BigDecimal b) {
        return compare(a, b) >= 0;
    }

    /** 折扣金额: 原价 * 折扣率(0.900 表示 9 折),半向上取整。 */
    public static BigDecimal discounted(BigDecimal amount, BigDecimal rate) {
        return multiply(amount, rate);
    }
}
