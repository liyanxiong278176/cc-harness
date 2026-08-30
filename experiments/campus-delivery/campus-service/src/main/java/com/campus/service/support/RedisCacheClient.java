package com.campus.service.support;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.util.concurrent.TimeUnit;

/**
 * Redis 缓存实现(app.cache.type=redis)。
 */
@Component
@ConditionalOnProperty(name = "app.cache.type", havingValue = "redis", matchIfMissing = true)
public class RedisCacheClient implements CacheClient {

    private final StringRedisTemplate redis;

    public RedisCacheClient(StringRedisTemplate redis) {
        this.redis = redis;
    }

    @Override
    public String get(String key) {
        return redis.opsForValue().get(key);
    }

    @Override
    public void set(String key, String value, long ttlSeconds) {
        redis.opsForValue().set(key, value, ttlSeconds, TimeUnit.SECONDS);
    }

    @Override
    public boolean del(String key) {
        return Boolean.TRUE.equals(redis.delete(key));
    }

    @Override
    public boolean exists(String key) {
        return Boolean.TRUE.equals(redis.hasKey(key));
    }

    @Override
    public boolean setIfAbsent(String key, String value, long ttlSeconds) {
        return Boolean.TRUE.equals(redis.opsForValue().setIfAbsent(key, value, ttlSeconds, TimeUnit.SECONDS));
    }

    @Override
    public Long increment(String key, long delta, long ttlSeconds) {
        Long v = redis.opsForValue().increment(key, delta);
        redis.expire(key, ttlSeconds, TimeUnit.SECONDS);
        return v;
    }

    @Override
    public Long decrement(String key, long delta, long ttlSeconds) {
        Long v = redis.opsForValue().decrement(key, delta);
        redis.expire(key, ttlSeconds, TimeUnit.SECONDS);
        return v;
    }
}
