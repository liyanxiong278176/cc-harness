package com.campus.common.util;

import java.util.regex.Pattern;

/**
 * 脱敏工具(仅用于出参/日志)。
 */
public final class MaskUtils {

    private static final Pattern PHONE = Pattern.compile("(\\d{3})\\d{4}(\\d{4})");

    private MaskUtils() {
    }

    /** 13812345678 -> 138****5678;不足 7 位原样返回。 */
    public static String phone(String phone) {
        if (phone == null || phone.length() < 7) {
            return phone;
        }
        return PHONE.matcher(phone).replaceAll("$1****$2");
    }

    /** 姓名: 保留首字,其余 *;空返回原值。 */
    public static String name(String name) {
        if (name == null || name.isEmpty()) {
            return name;
        }
        if (name.length() == 1) {
            return name;
        }
        return name.charAt(0) + "*".repeat(name.length() - 1);
    }
}
