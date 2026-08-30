package com.campus.common.auth;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.SignatureAlgorithm;
import io.jsonwebtoken.security.Keys;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.util.Date;
import java.util.HashMap;
import java.util.Map;

/**
 * JWT 工具(HS256)。
 * 密钥来自环境变量 APP_JWT_SECRET(≥32 字节),缺失时用开发默认值(仅 dev)。
 */
public final class JwtUtils {

    private static final long EXPIRE_MS = 7 * 24 * 3600 * 1000L;
    private static final String DEFAULT_SECRET = "campus-delivery-dev-secret-key-change-me-0123456789";

    private JwtUtils() {
    }

    private static SecretKey key() {
        String s = System.getenv("APP_JWT_SECRET");
        if (s == null || s.length() < 32) {
            s = DEFAULT_SECRET;
        }
        return Keys.hmacShaKeyFor(s.getBytes(StandardCharsets.UTF_8));
    }

    public static String createToken(Long userId, String username, String role) {
        Map<String, Object> claims = new HashMap<>();
        claims.put("uid", userId);
        claims.put("role", role);
        Date now = new Date();
        return Jwts.builder()
                .setClaims(claims)
                .setSubject(username)
                .setIssuedAt(now)
                .setExpiration(new Date(now.getTime() + EXPIRE_MS))
                .signWith(key(), SignatureAlgorithm.HS256)
                .compact();
    }

    /**
     * 解析并校验签名与有效期。
     * 非法 token(签名被篡改 / 格式非法 / 过期)直接抛出 {@link io.jsonwebtoken.JwtException} 子类异常,
     * 由调用方决定处理策略;需要 null 判定的场景请用 {@link #parseOrNull(String)}。
     */
    public static Claims parse(String token) {
        return Jwts.parserBuilder().setSigningKey(key()).build()
                .parseClaimsJws(token).getBody();
    }

    /** 宽容版:非法 token(篡改/垃圾/过期)返回 null,不抛异常。 */
    public static Claims parseOrNull(String token) {
        try {
            return parse(token);
        } catch (Exception e) {
            return null;
        }
    }

    public static Long uid(Claims c) {
        return c.get("uid", Long.class);
    }

    public static String role(Claims c) {
        return c.get("role", String.class);
    }
}
