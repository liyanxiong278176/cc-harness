package com.campus.service.support;

import java.util.concurrent.TimeUnit;

/**
 * 缓存抽象。生产用 Redis,测试/无 Redis 环境用内存实现;
 * 服务层只依赖本接口,保证可替换与可测。
 */
public interface CacheClient {

    String get(String key);

    void set(String key, String value, long ttlSeconds);

    boolean del(String key);

    boolean exists(String key);

    /** 仅在键不存在时写入(幂等去重),返回是否写入成功。 */
    boolean setIfAbsent(String key, String value, long ttlSeconds);

    Long increment(String key, long delta, long ttlSeconds);

    Long decrement(String key, long delta, long ttlSeconds);
}
