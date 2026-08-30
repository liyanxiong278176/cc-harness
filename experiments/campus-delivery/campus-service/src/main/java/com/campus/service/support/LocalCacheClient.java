package com.campus.service.support;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * 内存缓存实现(app.cache.type=local;测试与无 Redis 环境使用)。
 * 语义与 Redis 实现一致(含 TTL 清理),便于集成测试替换。
 */
@Component
@ConditionalOnProperty(name = "app.cache.type", havingValue = "local")
public class LocalCacheClient implements CacheClient {

    private static final class Entry {
        String value;
        long expireAt;

        Entry(String value, long expireAt) {
            this.value = value;
            this.expireAt = expireAt;
        }

        boolean expired() {
            return expireAt <= System.currentTimeMillis();
        }
    }

    private final Map<String, Entry> store = new ConcurrentHashMap<>();
    private final ScheduledExecutorService cleaner = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "local-cache-cleaner");
        t.setDaemon(true);
        return t;
    });

    public LocalCacheClient() {
        cleaner.scheduleAtFixedRate(this::sweep, 60, 60, TimeUnit.SECONDS);
    }

    private void sweep() {
        long now = System.currentTimeMillis();
        store.entrySet().removeIf(e -> e.getValue().expired());
    }

    @Override
    public String get(String key) {
        Entry e = store.get(key);
        if (e == null) {
            return null;
        }
        if (e.expired()) {
            store.remove(key);
            return null;
        }
        return e.value;
    }

    @Override
    public void set(String key, String value, long ttlSeconds) {
        store.put(key, new Entry(value, System.currentTimeMillis() + ttlSeconds * 1000));
    }

    @Override
    public boolean del(String key) {
        return store.remove(key) != null;
    }

    @Override
    public boolean exists(String key) {
        return get(key) != null;
    }

    @Override
    public boolean setIfAbsent(String key, String value, long ttlSeconds) {
        Entry e = new Entry(value, System.currentTimeMillis() + ttlSeconds * 1000);
        Entry old = store.putIfAbsent(key, e);
        return old == null;
    }

    @Override
    public Long increment(String key, long delta, long ttlSeconds) {
        synchronized (store) {
            String cur = get(key);
            long v = cur == null ? 0 : Long.parseLong(cur);
            v += delta;
            set(key, String.valueOf(v), ttlSeconds);
            return v;
        }
    }

    @Override
    public Long decrement(String key, long delta, long ttlSeconds) {
        return increment(key, -delta, ttlSeconds);
    }
}
