# 校园外卖系统 — 架构与技术规约

- 版本: 1.0
- 日期: 2025-08-29
- 决策记录: 见 `docs/design-decisions.md`

## 1. 系统概述

单体(Monolith)校园外卖系统,经典分层架构,面向校内学生(用户端)与校园商户(商家管理端)。
覆盖七个业务域:**用户、商家、订单、模拟支付、模拟配送、评价、站内通知**。

生产代码标准(无需真实上线):统一响应/全局异常/规范错误码、参数校验、JWT 鉴权与接口权限、
参数化 SQL、敏感字段加密脱敏、缓存一致性、库存防超卖、MQ 幂等/重试/死信、事务边界、
操作日志、审计字段、并发处理、单元/集成/前端测试、Docker Compose 一键启动与运行手册。

## 2. 技术栈

| 层 | 技术 | 版本 | 说明 |
|----|------|------|------|
| JDK | OpenJDK | 17 | 语言运行时 |
| 框架 | Spring Boot | 3.2.x | Web + Validation + AOP + AMQP |
| ORM | MyBatis-Plus | 3.5.5 | 参数化 SQL、逻辑删除、乐观锁、分页 |
| 数据库 | MySQL | 8.0 | utf8mb4, InnoDB, 事务 |
| 缓存 | Redis | 7.x | 缓存 + 幂等去重 + 库存预扣 |
| 消息 | RabbitMQ | 3.12.x | 订单事件、通知、死信 |
| 前端 | React + Ant Design 5 + Vite | 18.x / 5.x / 5.x | 用户端 + 商家端同一 SPA |
| 测试 | JUnit 5 / Mockito / MockMvc / H2 / Vitest / RTL | - | 单元 + 集成 + 前端关键交互 |
| 构建 | Maven 多模块 | 3.9.x | 见 §5 |
| 部署 | Docker Compose | v2 | MySQL/Redis/RabbitMQ/App |

## 3. 总体架构图

```text
┌───────────────────────────── Browser (React + AntD SPA) ─────────────────────────────┐
│   /user/*  用户端(首页/店铺/购物车/结算/订单/地址/优惠券/评价/通知)                       │
│   /merchant/*  商家管理端(菜品/分类/库存/营业/订单/评价)                                 │
└───────────────────────────────────┬──────────────────────────────────────────────────┘
                                    │ HTTP/JSON (Bearer JWT)
┌───────────────────────────────────▼──────────────────────────────────────────────────┐
│  campus-web (Controller 层 + 安全)                                                    │
│  JwtAuthInterceptor → @RequireRole → @Valid 参数校验 → Result<T> 统一响应              │
│  GlobalExceptionHandler(规范错误码)   OperationLogAspect(操作日志)                     │
└───────────────────────────────────┬──────────────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼──────────────────────────────────────────────────┐
│  campus-service (Service 层,事务边界)                                                 │
│  用户/地址/优惠券 · 商家/菜品/库存 · 购物车/订单/支付 · 配送 · 评价 · 通知              │
│  adapter: MockPaymentGateway / MockSmsSender / MockRiderDispatcher (可替换)           │
└──────┬────────────────────────────┬─────────────────────────────┬────────────────────┘
       │                            │                             │
┌──────▼───────┐          ┌─────────▼──────────┐         ┌────────▼─────────┐
│ campus-dao   │          │ campus-mq (campus-  │         │ Redis (缓存/去重/  │
│ MyBatis-Plus │          │ service 内 MQ 层)    │         │ 库存预扣)          │
│ 实体/Mapper/ │          │ 可靠投递 outbox +    │         └──────────────────┘
│ 审计/乐观锁   │          │ 幂等消费/重试/DLX    │
└──────┬───────┘          └─────────┬──────────┘
       │                            │
┌──────▼────────────────────────────▼──────────────────────────────────────────────────┐
│  campus-common (统一错误码/Result/异常/加密脱敏/常量/工具)                              │
│  MQ:  RabbitMQ (订单事件/通知/死信)    DB: MySQL 8 (业务数据)                          │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

依赖方向(单向,禁止反向):`web → service → {dao, mq} → common`;`service → dao/common`;`mq → dao/common`。
`dao`/`mq` 均不得依赖 `service`/`web`;`common` 不依赖任何业务模块。由 `tools/static-analysis/layer_check.py` 校验。

## 4. 分层职责与边界

| 层 | 职责 | 禁止 |
|----|------|------|
| Controller(web) | 参数接收/校验、调用 Service、返回 Result | 直接操作 DAO/Redis/MQ、写业务规则 |
| Service | 业务规则、事务边界、编排、调用 adapter/MQ/Redis | 拼接 SQL、暴露 DAO 细节、Http 细节 |
| DAO | 实体、Mapper、参数化 SQL、分页/乐观锁/逻辑删除 | 业务判断 |
| MQ 层 | 事件发布(outbox)、消费(幂等)、重试、死信 | 业务决策 |
| Common | 错误码、统一返回、异常、加密脱敏、工具、常量 | 依赖业务模块 |

事务边界约定:写事务必须声明 `@Transactional(rollbackFor = Exception.class)`,且只放在 Service
公开方法;禁止在 Controller 开启事务;MQ 消费方法独立事务且幂等,见 `docs/mq-idempotency.md`。

## 5. Maven 多模块

```text
campus-delivery (parent pom)
├── campus-common   统一返回/错误码/异常/加密脱敏/工具/常量
├── campus-dao      实体 / Mapper / MyBatis-Plus 配置
├── campus-service  业务服务 / 模拟适配器 / MQ 生产消费 / Redis 缓存
└── campus-web      Controller / 安全拦截 / 全局异常 / 应用入口 / 配置
```

`mvn -pl campus-web -am package` 产出可执行 fat jar;`Dockerfile` 多阶段构建。

## 6. 关键策略摘要(详见专门文档)

- 库存防超卖与缓存一致性: `docs/cache-consistency.md`
- MQ 幂等/重试/死信: `docs/mq-idempotency.md`
- 安全(加密/脱敏/JWT/权限): `docs/security.md`
- 运行手册与故障排查: `docs/operations.md`

## 7. 模拟外部能力(可替换适配器)

| 能力 | 接口 | 默认实现 | 替换点 |
|------|------|----------|--------|
| 支付 | `PaymentGateway` | `MockPaymentGateway`(本地生成交易号+同步回写,可模拟失败/超时) | 换真实网关实现同接口 |
| 短信 | `SmsSender` | `MockSmsSender`(日志输出验证码) | 换短信服务商 |
| 配送 | `RiderDispatcher` | `MockRiderDispatcher`(轮询骑手池分配) | 换真实调度 |

所有适配器通过 Spring 接口注入,`@ConditionalOnProperty` 可在配置切换。

## 8. 环境与运行(详见 docs/operations.md)

Docker Compose 启动 MySQL 8 / Redis 7 / RabbitMQ 3.12 / 应用;`scripts/start.sh` 一键启动,
`scripts/healthcheck.sh` 健康检查,迁移脚本 `db/init/01-schema.sql`、`02-seed.sql` 自动执行。
