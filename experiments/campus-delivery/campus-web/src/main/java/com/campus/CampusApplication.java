package com.campus;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.transaction.annotation.EnableTransactionManagement;

/**
 * 校园外卖系统启动类(单体)。
 *
 * <p>主类位于 {@code com.campus} 根包,使 {@code @SpringBootApplication} 的默认
 * 组件扫描覆盖 campus-web(com.campus.web.*)、campus-service(com.campus.service.*)
 * 与 campus-common 中的 Spring 组件;DAO 由 {@code @MapperScan} 显式注册。</p>
 *
 * <p>{@code @EnableScheduling}: campus-service 的 MqRelayJob 依赖 {@code @Scheduled}。
 * {@code @EnableTransactionManagement}: 事务注解驱动(Service 层事务边界)。</p>
 */
@SpringBootApplication
@EnableScheduling
@EnableTransactionManagement
@MapperScan("com.campus.dao.mapper")
public class CampusApplication {

    public static void main(String[] args) {
        SpringApplication.run(CampusApplication.class, args);
    }
}
