# 缓存一致性 & 库存防超卖策略

## 1. 缓存一致性

### 1.1 缓存范围(只读热点)
- `cache:merchant:{id}` — 商家详情
- `cache:menu:{merchantId}` — 上架菜品(按分类分组)
- `cache:cart:{userId}` — 购物车(可缓存可不用,DB 为准)
- 订单/支付/库存等写密集数据**不缓存**。

### 1.2 一致性策略: delete-on-write(写后失效)
1. 所有写操作(改价/改库存/上下架/改商家信息)在**事务提交后**调用 `DishCacheManager.evict(merchantId)`,
   删除 `cache:merchant:{id}` 与 `cache:menu:{merchantId}`。
2. 不采用"写缓存"模式,避免 DB 与缓存双写不一致;缓存只是 DB 的加速视图。
3. 设置 TTL(商家 10min,菜单 10min)兜底,即使删除失败也会过期自愈。
4. 失效时机放在事务提交后(TransactionSynchronization.afterCommit),防止回滚后误删。

### 1.3 读路径
`getMenu(merchantId)`: 先查缓存,命中直接返回;未命中查 DB → 写缓存(带 TTL)→ 返回。
菜品价格/库存下单时以 DB 为准(下单事务内重读),缓存仅用于列表展示,保证不因缓存读到旧价导致金额错误。

## 2. 库存防超卖

### 2.1 模型
`dish.stock` 为可售库存(数据库字段,带 `version` 乐观锁);`stock_change_log` 记录每次变动审计。

### 2.2 下单扣减(事务内,原子)
```sql
UPDATE dish
   SET stock = stock - #{qty},
       sold_count = sold_count + #{qty},
       version = version + 1
 WHERE id = #{dishId}
   AND status = 1
   AND stock >= #{qty}
   AND version = #{expectedVersion}   -- 可选,行锁已保证
```
影响行数 = 0 → 库存不足或已下架 → 抛 `300103`(回滚整个下单事务)。
每道菜逐项执行,同一行由 InnoDB 行锁串行化,天然防超卖;`version` 提供可观测的并发冲突信号。

### 2.3 回滚
- 订单取消(未支付): 同事务内 `stock = stock + qty` 回补并写 ROLLBACK 流水。
- 下单事务失败: 数据库回滚自动还原。
- 超时未支付(定时任务): 回补库存并写流水、关单。

### 2.4 Redis 的作用(预检,非强一致)
- 下单前 `DECRBY stock:pre:{dishId}` 预扣做快速拒绝(如库存已 0 直接提示);
- 最终一致性以 DB 为准;Redis 预扣与 DB 回滚之间允许短暂不一致(由 DB 兜底),
  预扣键在订单取消/关单时 INCRBY 回补,并设置过期时间防止泄漏。

### 2.5 并发验证
- Java 单测: `InventoryServiceTest`(Mockito 验证影响行数分支)。
- 参考逻辑: `tools/logic-harness` 复刻扣减算法,`node:test` 并发模拟 100 线程抢 50 库存,验证不超卖。

## 3. 关键结论
- 缓存与 DB 的一致性: 写后失效 + TTL 兜底,允许秒级滞后,下单金额一律读 DB。
- 防超卖: 数据库行级原子更新为准,Redis 仅预检;无分布式锁,无锁竞争热点。
