# MQ 可靠投递、消费幂等、重试与死信

## 1. 拓扑

```text
            ┌────────────── 业务事务 ──────────────┐
Service ──▶ │ 写业务表 + mq_message(PENDING)       │
            └──────────────────┬───────────────────┘
                               │ 事务提交后
                               ▼
                    MqPublisher(本地表 SENT)──▶ RabbitMQ
                                                          │
        ┌──────────────────────┬─────────────────────────┤
        ▼                      ▼                         ▼
 exchange.campus.order   exchange.campus.notify    exchange.campus.dlx
 queue.order.events      queue.notify.*            queue.notify.dlq / order.dlq
        │                      │
        ▼                      ▼
  OrderEventConsumer    NotificationConsumer(幂等,重试)
```

- 交换机: `campus.exchange.order`(topic), `campus.exchange.notify`(topic), `campus.exchange.dlx`(direct)
- 队列: `queue.order.events`, `queue.notify.order`, `queue.notify.payment`, `queue.notify.delivery`,
  `queue.order.dlq`, `queue.notify.dlq`
- 死信: 队列声明 `x-dead-letter-exchange=campus.exchange.dlx`,消费失败重试上限后由 DLQ 落库告警。

## 2. 可靠投递(outbox)

1. 业务事务内同时写 `mq_message`(msg_id 唯一,payload=事件 JSON),与业务同库同事务 → 不丢消息。
2. 事务提交后 `MqPublisher.send(mqMessage)` 投递并置 SENT;投递失败保持 PENDING。
3. 定时任务(`MqRetryScheduler`)扫描 `status=PENDING AND next_retry_time<=now`,重投,重试次数封顶
   (3 次)后置 FAILED 并告警(operation_log 记录)。投递侧重试天然幂等(msg_id 幂等,见下)。

## 3. 消费幂等

- 每条消息带 `msgId`(UUID)与业务幂等键 `userId+bizType+bizId`(如 `10001+ORDER_STATUS+orderNo`)。
- 消费流程:
  1. Redis `SETNX notify:dedup:{userId}:{bizType}:{bizId}` 去重(快速路径);
  2. DB 唯一键 `notification.uk_dedup(user_id,biz_type,biz_id)` 兜底:重复插入抛 DuplicateKeyException,
     捕获后视为已处理,返回 ack;
  3. 消费逻辑在独立事务中执行,事务提交后删除 Redis 去重键(防止"事务回滚但键已置位")。
- 结论: Redis 加速 + DB 唯一键最终幂等,消息乱序/重复/并发消费均安全。

## 4. 重试与死信

- Consumer 使用手动 ack(`AcknowledgeMode.MANUAL`)。
- 业务异常: 重试指数退避(1s/5s/30s,共 3 次,通过 `requeue-rejected=false` + delay? 简化: 抛异常让
  Spring AMQP 按 `defaultRequeueRejected` 重试;超过 `x-death` 计数则手动拒绝不重入队)。
- 死信: 消费彻底失败 → `basicNack(requeue=false)` → 路由 `campus.exchange.dlx` → DLQ;
  DLQ 消费者记录告警(operation_log/日志)并停止重试(人工介入),可验证消息在 `queue.notify.dlq` 积压。
- 验证方式: 文档 `docs/operations.md` §5 提供 RabbitMQ 控制台查看 x-death 计数与 DLQ 积压的步骤;
  逻辑复刻于 `tools/logic-harness/mq-idempotency.test.js`(模拟重试上限与死信路由)。

## 5. 消息类型

| 事件 | routing key | 消费方 | 幂等键 |
|------|-------------|--------|--------|
| 订单创建 | order.created | OrderEventConsumer(预留,通知用户) | userId+ORDER_STATUS+orderNo |
| 订单支付成功 | order.paid | 通知用户"支付成功" | userId+PAYMENT+orderNo |
| 订单状态变更 | order.status.changed | 通知用户状态更新 | userId+ORDER_STATUS+orderNo |
| 配送状态变更 | delivery.status.changed | 通知用户配送更新 | userId+DELIVERY+orderNo |
| 退款结果 | refund.result | 通知用户退款结果 | userId+SYSTEM+orderNo |

## 6. 一致性保证

- 投递: 业务与 mq_message 同事务,事务提交才投递,不丢、不乱序(单队列 FIFO 尽力)。
- 消费: DB 唯一键幂等,重复消费不产生重复通知。
- 失败: 重试有上限,超过进 DLQ,人工/定时补偿,不静默丢失。
