package com.campus.web.auth;

import com.campus.common.api.Result;
import com.campus.common.api.ResultCode;
import com.campus.common.auth.JwtUtils;
import com.campus.common.auth.RequireRole;
import com.campus.common.auth.UserContext;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.jsonwebtoken.Claims;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/**
 * JWT 鉴权拦截器。
 *
 * <ul>
 *   <li>解析 {@code Authorization: Bearer <jwt>} → {@link JwtUtils#parse} 校验签名/有效期;失败 40101;</li>
 *   <li>解析成功后将 {@code UserInfo} 写入 {@link UserContext}(ThreadLocal),请求结束清理;</li>
 *   <li>处理类/方法标注 {@link RequireRole} 时校验角色,不满足返回 40301;未标注 = 仅需登录;</li>
 *   <li>公开接口(login/register/商家浏览/模拟支付回调/health)在 {@code WebConfig} 中排除。</li>
 * </ul>
 */
@Component
public class JwtAuthInterceptor implements HandlerInterceptor {

    private static final Logger log = LoggerFactory.getLogger(JwtAuthInterceptor.class);
    private static final String BEARER_PREFIX = "Bearer ";

    private final ObjectMapper objectMapper;

    public JwtAuthInterceptor(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler)
            throws IOException {
        if (!(handler instanceof HandlerMethod handlerMethod)) {
            return true;
        }

        String token = resolveToken(request);
        Claims claims = (token == null) ? null : JwtUtils.parseOrNull(token);
        if (claims == null) {
            log.debug("[jwt] unauthorized request: {} {}", request.getMethod(), request.getRequestURI());
            return reject(response, ResultCode.UNAUTHORIZED);
        }

        Long uid = JwtUtils.uid(claims);
        if (uid == null) {
            return reject(response, ResultCode.UNAUTHORIZED);
        }
        UserContext.set(new UserContext.UserInfo(uid, claims.getSubject(), JwtUtils.role(claims)));

        if (!hasRequiredRole(handlerMethod)) {
            UserContext.clear();
            return reject(response, ResultCode.FORBIDDEN);
        }
        return true;
    }

    @Override
    public void afterCompletion(HttpServletRequest request, HttpServletResponse response,
                                Object handler, Exception ex) {
        UserContext.clear();
    }

    private static String resolveToken(HttpServletRequest request) {
        String header = request.getHeader(HttpHeaders.AUTHORIZATION);
        if (header != null && header.startsWith(BEARER_PREFIX)) {
            String token = header.substring(BEARER_PREFIX.length()).trim();
            return token.isEmpty() ? null : token;
        }
        return null;
    }

    /** 方法级 @RequireRole 优先,其次类级;未标注返回 true(仅需登录)。 */
    private static boolean hasRequiredRole(HandlerMethod handlerMethod) {
        RequireRole mr = handlerMethod.getMethodAnnotation(RequireRole.class);
        if (mr == null) {
            mr = handlerMethod.getBeanType().getAnnotation(RequireRole.class);
        }
        if (mr == null || mr.value().length == 0) {
            return true;
        }
        return Arrays.asList(mr.value()).contains(UserContext.role());
    }

    private boolean reject(HttpServletResponse response, ResultCode rc) throws IOException {
        response.setStatus(HttpServletResponse.SC_OK);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        objectMapper.writeValue(response.getWriter(), Result.fail(rc));
        return false;
    }
}
