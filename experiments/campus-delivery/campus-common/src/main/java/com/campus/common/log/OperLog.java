package com.campus.common.log;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 操作日志标注: 由 AOP 切面(service 模块)记录到 operation_log。
 * 参数/结果快照会脱敏处理(手机号字段名匹配时打码)。
 */
@Target(ElementType.METHOD)
@Retention(RetentionPolicy.RUNTIME)
public @interface OperLog {

    /** 操作名(如 "结算下单")。 */
    String value();

    /** 所属模块(如 "order")。 */
    String module() default "";
}
