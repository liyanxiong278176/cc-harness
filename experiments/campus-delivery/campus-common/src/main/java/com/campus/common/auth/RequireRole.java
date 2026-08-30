package com.campus.common.auth;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 接口角色权限标注。作用于 Controller 类或方法;
 * 由 web 层拦截器校验(不满足返回 40301)。未标注 = 仅需登录。
 */
@Target({ElementType.METHOD, ElementType.TYPE})
@Retention(RetentionPolicy.RUNTIME)
public @interface RequireRole {

    /** 允许的角色(Constants.UserRole),多个为或关系。 */
    String[] value();
}
