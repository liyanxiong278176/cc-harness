package com.campus.web.config;

import com.campus.web.auth.JwtAuthInterceptor;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Web MVC 配置:注册 JWT 鉴权拦截器。
 *
 * <p>放行接口(docs/api.md 中公开或模拟渠道):</p>
 * <ul>
 *   <li>{@code POST /api/auth/register}、{@code POST /api/auth/login} — 公开;</li>
 *   <li>{@code /api/merchants/**} — 公开/登录;</li>
 *   <li>{@code /api/payment/mock/notify} — 模拟支付渠道回调(内部/手动);</li>
 *   <li>{@code /api/health} — 健康检查。</li>
 * </ul>
 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    private final JwtAuthInterceptor jwtAuthInterceptor;

    public WebConfig(JwtAuthInterceptor jwtAuthInterceptor) {
        this.jwtAuthInterceptor = jwtAuthInterceptor;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(jwtAuthInterceptor)
                .addPathPatterns("/api/**")
                .excludePathPatterns(
                        "/api/auth/login",
                        "/api/auth/register",
                        "/api/merchants",
                        "/api/merchants/**",
                        "/api/payment/mock/notify",
                        "/api/health");
    }
}
