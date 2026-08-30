# 设计决策记录 (ADR)

维护本项目的技术决策、失败原因与修复。格式:编号 + 状态 + 上下文 + 决策 + 后果。
项目记忆(供 Agent 复用)见 `observability/memory.md`。

---

## ADR-001 单体分层 + Maven 多模块(已接受)
- **上下文**: 需在"经典单体分层 Controller→Service→DAO→MQ→Common"下同时满足生产级质量与可维护性。
- **决策**: 物理上拆 4 个 Maven 模块(campus-common/dao/service/web),逻辑上保持单一可执行 jar;
  MQ 作为 service 包内独立子层(mq package),依赖方向单向: web→service→{dao,mq}→common。
- **后果**: 依赖边界可静态校验(layer_check.py);单 jar 便于 Docker 部署。

## ADR-002 鉴权:拦截器 + JWT + @RequireRole,而非 Spring Security 全家桶(已接受)
- **上下文**: 需要"JWT 鉴权和接口权限完整"且实现可控、依赖精简。
- **决策**: 自实现 JwtAuthInterceptor + UserContext(ThreadLocal),`@RequireRole` 注解做角色校验;
  密码使用 spring-security-crypto 的 BCrypt(仅引入 crypto,不引入整个 Security)。
- **后果**: 行为透明可控;注意:拦截器方式需自行保证登录接口放行。

## ADR-003 敏感字段:手机号 AES 加密存储 + 响应脱敏(已接受)
- **上下文**: 生产红线要求敏感字段加密/脱敏。
- **决策**: 手机号入库前 AES-128-GCM 加密(密钥来自环境变量 `APP_CRYPTO_KEY`,compose/.env 注入),
  出参统一 MaskUtils 脱敏(如 138****1234);加密在 Service 层集中处理,脱敏在 DTO 层。
- **后果**: 日志/接口永不出现明文手机号;AES 密钥为敏感配置,禁止入库入文档(observability 记录仅存摘要)。

## ADR-004 库存防超卖:DB 乐观锁 + 库存预检,不引入分布式锁(已接受)
- **上下文**: 需防超卖且策略明确、可解释。
- **决策**: 下单事务内 `UPDATE dish SET stock=stock-?, version=version+1 WHERE id=? AND stock>=? AND version=?`
  逐项扣减,影响行数=0 即冲突(300103 库存不足);Redis 仅做热点预检与缓存;扣减与订单创建同事务,
  取消订单同事务回滚库存并写 stock_change_log。见 docs/cache-consistency.md。
- **后果**: 单机数据库下精确防超卖;并发扣减正确性由数据库行锁+乐观条件保证(逻辑在 logic-harness 中复刻验证)。

## ADR-005 支付回调幂等:渠道交易号唯一键 + Redis 去重(已接受)
- **上下文**: 模拟支付回调可能重复/乱序,必须幂等。
- **决策**: payment_record.trade_no 唯一约束为最终幂等锚点;回调处理前 SETNX Redis 去重键
  `pay:dedup:{tradeNo}`;处理时 `SELECT ... FOR UPDATE` 锁支付记录,重复回调直接返回成功。
- **后果**: 幂等有 DB 唯一键兜底,Redis 仅加速;验证见 logic-harness 幂等场景。

## ADR-006 MQ 可靠性:本地消息表(outbox)+ 消费幂等 + 重试/死信(已接受)
- **上下文**: "消息消费幂等、重试和死信处理可验证"。
- **决策**: 业务事务内写 mq_message(PENDING),事务提交后异步投递并置 SENT(定时补偿扫描 PENDING);
  消费端以 `userId+bizType+bizId` 唯一键(notification.uk_dedup)幂等,重复消息跳过;
  consumer 手动 ack,失败按 `x-death` 计数重试(requeue 上限)后路由 DLX 死信队列。
  见 docs/mq-idempotency.md。
- **后果**: 投递与消费双幂等,可观测(retry_count/x-death);实现复杂度在 mq 层内封闭。

## ADR-007 订单状态机:显式枚举 + 版本号乐观锁流转(已接受)
- **上下文**: 状态流转并发安全、可审计。
- **决策**: OrderStatus 枚举定义允许迁移表;所有流转走 `orderService.updateStatus(expect, target)`,
  SQL 带 `status=? AND version=?` 条件,0 行更新抛 400105;每次流转写 operation_log。
- **后果**: 非法流转被数据库条件拒绝;状态机验证见 logic-harness。

## ADR-008 模拟外部能力:接口 + 默认实现 + ConditionalOnProperty(已接受)
- **上下文**: 支付/短信/配送不依赖真实第三方,但可替换。
- **决策**: PaymentGateway/SmsSender/RiderDispatcher 接口,`Mock*` 默认实现;
  `app.adapter.payment=mock` 等开关切换,新增真实实现只需实现接口并注册 bean。
- **后果**: 可测、可替换、无外部依赖。

## ADR-009 缓存一致性:缓存仅缓存读多写少的菜单数据,写路径主动失效(已接受)
- **上下文**: Redis 缓存菜品/商家,需明确一致性策略。
- **决策**: 只缓存"商家+上架菜品"(只读热点);写操作(改价/改库存/上下架)事务提交后删除对应缓存键
  (delete-on-write),不更新缓存(避免写缓存与 DB 双写不一致);设置短 TTL 兜底。
  见 docs/cache-consistency.md。
- **后果**: 一致性模型简单(缓存视为 DB 的加速视图,允许秒级滞后),无双写窗口。

## ADR-010 测试策略(已接受)
- **上下文**: 单元/核心接口集成/前端关键交互测试。
- **决策**: 纯逻辑(状态机/金额计算/幂等/防超卖)JUnit5 单元测试;web 层 MockMvc + H2(MySQL 模式)
  集成测试,Redis/RabbitMQ 用 MockBean;前端 Vitest + Testing Library 关键交互。
- **后果**: 不依赖 Docker/真实中间件即可跑测试;真实中间件验证由 compose 环境运行手册覆盖。

## ADR-011 沙箱环境限制与验证边界(已接受,重要)
- **上下文**: 运行环境无 JDK/Maven/Docker,外网仅 github.com+pypi.org 可达(见 observability/environment-audit.json)。
- **决策**: 全量交付源代码与文档;沙箱内执行可运行的静态检查(layer_check/sql_audit/json/结构检查)
  与 Node 参考逻辑测试(node:test)作为真实证据;编译/集成/容器健康等验证在外部环境按
  docs/operations.md 复现,结果不在此环境宣称完成。
- **后果**: 诚实区分"已验证"与"未验证";不伪造构建/测试结果。
