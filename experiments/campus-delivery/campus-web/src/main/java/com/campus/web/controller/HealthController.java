package com.campus.web.controller;

import com.campus.common.api.Result;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.sql.DataSource;
import java.sql.Connection;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 健康检查(docs/operations.md):{@code GET /api/health}。
 * 各组件探针失败仅标记 DOWN,不影响接口返回(便于启动自检)。
 */
@RestController
@RequestMapping("/api/health")
public class HealthController {

    private static final Logger log = LoggerFactory.getLogger(HealthController.class);

    private final DataSource dataSource;
    private final StringRedisTemplate redisTemplate;
    private final RabbitTemplate rabbitTemplate;

    public HealthController(DataSource dataSource, StringRedisTemplate redisTemplate,
                            RabbitTemplate rabbitTemplate) {
        this.dataSource = dataSource;
        this.redisTemplate = redisTemplate;
        this.rabbitTemplate = rabbitTemplate;
    }

    @GetMapping
    public Result<Map<String, Object>> health() {
        Map<String, Object> components = new LinkedHashMap<>();
        components.put("db", checkDb());
        components.put("redis", checkRedis());
        components.put("rabbit", checkRabbit());
        boolean up = !"DOWN".equals(components.get("db"));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", up ? "UP" : "DOWN");
        body.put("components", components);
        body.put("version", "1.0.0");
        body.put("time", LocalDateTime.now().toString());
        return Result.success(body);
    }

    private String checkDb() {
        try (Connection c = dataSource.getConnection()) {
            return c.isValid(2) ? "UP" : "DOWN";
        } catch (Exception e) {
            log.warn("[health] db DOWN: {}", e.getMessage());
            return "DOWN";
        }
    }

    private String checkRedis() {
        try {
            redisTemplate.getConnectionFactory().getConnection().ping();
            return "UP";
        } catch (Exception e) {
            log.warn("[health] redis DOWN: {}", e.getMessage());
            return "DOWN";
        }
    }

    private String checkRabbit() {
        try {
            rabbitTemplate.execute(channel -> {
                channel.queueDeclarePassive("queue.notify.order");
                return true;
            });
            return "UP";
        } catch (Exception e) {
            log.warn("[health] rabbit DOWN: {}", e.getMessage());
            return "DOWN";
        }
    }
}
