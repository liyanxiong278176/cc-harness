package com.campus.common.auth;

import io.jsonwebtoken.Claims;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

class JwtUtilsTest {

    @Test
    void createAndParseRoundTrip() {
        String token = JwtUtils.createToken(42L, "zhangsan", "USER");
        assertNotNull(token);
        Claims claims = JwtUtils.parse(token);
        assertEquals(42L, JwtUtils.uid(claims));
        assertEquals("USER", JwtUtils.role(claims));
        assertEquals("zhangsan", claims.getSubject());
    }

    @Test
    void differentRolesParsed() {
        String token = JwtUtils.createToken(7L, "rider1", "RIDER");
        Claims claims = JwtUtils.parse(token);
        assertEquals("RIDER", JwtUtils.role(claims));
    }

    @Test
    void tamperedTokenRejected() {
        String token = JwtUtils.createToken(1L, "admin", "ADMIN");
        String tampered = token.substring(0, token.length() - 2) + "xx";
        assertThrows(Exception.class, () -> JwtUtils.parse(tampered));
    }

    @Test
    void garbageTokenRejected() {
        assertThrows(Exception.class, () -> JwtUtils.parse("not.a.jwt"));
    }
}
