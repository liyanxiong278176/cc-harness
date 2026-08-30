# 库存扣减与防超卖策略

> 目标: 并发下单不超卖、结果可审计、逻辑可独立验证。
> 独立验证参考: `logic-harness/stock-deduct.test.js`(与本文档语义一致的 Node 参考实现,真实可运行)。

## 1. 策略总览(三层防线)

| 层 | 机制 | 作用 | 失败代价 |
|----|------|------|----------|
| 1 | 业务预检(下单前) | 读取 dish.stock,不足直接返回 300103 | 仅减少无谓 DB 竞争 |
| 2 | **事务内条件更新(最终防线)** | `UPDATE dish SET stock=stock-?, sold_count=sold_count+?, version=version+1 WHERE id=? AND stock>=? AND version=?` | 防超卖的唯一事实来源 |
| 3 | Redis 预扣(可选热路径) | `DECR` 热点菜品,回滚时 `INCR` | 加速,非正确性依赖 |

第 2 层是权威: 数据库行锁 + `stock>=?` 条件保证任何并发下扣减不会低于 0;影响行数=0 即视为冲突,抛出 `STOCK_NOT_ENOUGH`。

## 2. 下单事务时序(OrderService.checkout)

```
@Transactional
checkout():
  for each cart item:
    dish = dishMapper.selectById(dishId)                      // 预检
    if dish.stock < qty -> throw STOCK_NOT_ENOUGH
  compute totals(coupon)                                      // 纯函数,见 logic-harness/price-calc
  insert order_info(快照, status=CREATED)
  insert order_item × n(快照)
  for each item:
    rows = dishMapper.deductStock(dishId, qty, version)       // 条件更新
    if rows == 0 -> throw STOCK_NOT_ENOUGH
    insert stock_change_log(DEDUCT, before/after)
  use coupon (user_coupon -> USED)                            // 条件更新 status=UNUSED AND version=?
  write mq_message(outbox, PENDING)                           // 事务内
  // 事务提交后由 outbox 补偿线程投递 order.created
```

任何一步异常 → 整体回滚: 已扣库存、券、订单、outbox 全部回滚,不存在部分扣减。

## 3. 取消/退款回滚库存

- 取消订单: `CREATED -> CANCELLED`,同事务 `UPDATE dish SET stock=stock+?`(无条件回滚,只增不减),写 `stock_change_log(ROLLBACK)`,券退回。
- 退款(已支付): 走退款流程,退款成功后恢复库存(见 refund 状态机),写 ROLLBACK 流水。

## 4. 缓存一致性(与库存的关系)

- Redis 缓存中的 stock 仅作展示;下单校验**不读缓存**,以 DB 为准。
- 扣减/回滚后删除缓存键 `cache:menu:{merchantId}`(delete-on-write),下次读取回源 DB 重建。
- 展示层短暂滞后(秒级)可接受,正确性由 DB 保证(见 docs/cache-consistency.md)。

## 5. 并发与事务边界

- 事务边界: `@Transactional` 只在 OrderService/PaymentService/RefundService 等写路径服务方法上;Controller 不开启事务。
- 锁顺序: 一律"先订单行/菜品行,后用户券行",避免死锁。
- 隔离级别: MySQL 默认 REPEATABLE_READ;条件更新 + 行锁已足够,无需串行化。
- 超时未支付自动取消: 定时任务扫描 `CREATED` 超过 N 分钟的订单 → 同事务回滚库存/券(幂等,仅 CREATED→CANCELLED 可流转)。
