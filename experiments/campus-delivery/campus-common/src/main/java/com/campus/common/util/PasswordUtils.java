package com.campus.common.util;

import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;

/**
 * 密码工具: BCrypt 散列(仅引入 spring-security-crypto,不引入完整 Security)。
 */
public final class PasswordUtils {

    private static final BCryptPasswordEncoder ENCODER = new BCryptPasswordEncoder();

    private PasswordUtils() {
    }

    public static String encode(String raw) {
        return ENCODER.encode(raw);
    }

    public static boolean matches(String raw, String hash) {
        if (raw == null || hash == null) {
            return false;
        }
        return ENCODER.matches(raw, hash);
    }
}
