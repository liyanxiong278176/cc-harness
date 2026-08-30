# campus-web — Spring Boot 启动模块

单体校园外卖系统的 Web 启动模块:Controller 层、JWT 鉴权拦截器、全局异常处理、
RabbitMQ 声明配置、MyBatis-Plus 配置与 `application.yml`。

## 模块职责与依赖

```
campus-common (统一响应/错误码/常量/JWT/脱敏)
campus-dao    (实体/Mapper)
campus-service(业务服务 / MQ 生产者消费者 / 模拟外部适配器)
campus-web    (本模块: HTTP 入口)
```

- 依赖 `campus-common`、`campus-dao`、`campus-service`,版本均为 `${project.version}`。
- 启动类 `com.campus.CampusApplication`(位于 `com.campus` 根包,默认组件扫描覆盖 web/service/common;
  `@MapperScan("com.campus.dao.mapper")` 注册 DAO)。
- 顶层 `pom.xml` 已声明 4 个模块(campus-common / campus-dao / campus-service / campus-web)。

## 启动方式

前置:JDK 17 + Maven 3.6+;MySQL 8 / Redis 7 / RabbitMQ 可用(或用 `app.mq.enabled=false` 关闭 MQ)。

```bash
# 方式一: 全量构建并启动
mvn -pl campus-web -am package
java -jar campus-web/target/campus-web-1.0.0.jar

# 方式二: 开发模式(父目录执行)
mvn -pl campus-web -am spring-boot:run

# 方式三: docker-compose(需宿主机具备网络,见 docs/operations.md)
./scripts/start.sh
```

- 服务地址:`http://localhost:8080`,所有接口前缀 `/api`(与 docs/api.md 一一对应)。
- 健康检查:`GET /api/health` → `{status, components:{db,redis,rabbit}, version, time}`。

## 接口与鉴权

| 域 | 前缀 | 角色 |
|----|------|------|
| 认证 | `/api/auth` | register/login 公开;me/password 需登录 |
| 用户 | `/api/user` | USER |
| 商家浏览 | `/api/merchants` | 公开/登录(无需 token) |
| 购物车 | `/api/cart` | USER |
| 订单 | `/api/orders` | USER |
| 模拟支付 | `/api/payment` | mock/notify 公开(模拟渠道) |
| 骑手 | `/api/rider` | RIDER |
| 商家管理 | `/api/merchant` | MERCHANT |

鉴权:`Authorization: Bearer <jwt>`。拦截器 `JwtAuthInterceptor` 解析并校验 token
(密钥 `APP_JWT_SECRET`,≥32 字节,缺失时使用 dev 默认值),将 `UserInfo` 写入
`UserContext`(ThreadLocal);处理类/方法标注 `@RequireRole` 时校验角色,不满足返回
`40301`;未标注 = 仅需登录。全局异常统一返回 `Result<T>`(`GlobalExceptionHandler`)。

## 配置项(环境变量覆盖)

| 环境变量 | 默认值(dev) | 说明 |
|----------|-------------|------|
| `SERVER_PORT` | `8080` | HTTP 端口 |
| `CAMPUS_DB_URL` | `jdbc:mysql://localhost:3306/campus_delivery?...` | JDBC 连接串 |
| `CAMPUS_DB_USERNAME` / `CAMPUS_DB_PASSWORD` | `root` / `root` | 数据库账号 |
| `CAMPUS_DB_DRIVER` | `com.mysql.cj.jdbc.Driver` | 驱动 |
| `CAMPUS_DB_POOL_MAX` | `20` | Hikari 最大连接数 |
| `CAMPUS_REDIS_HOST` / `CAMPUS_REDIS_PORT` | `localhost` / `6379` | Redis |
| `CAMPUS_REDIS_PASSWORD` / `CAMPUS_REDIS_DATABASE` | 空 / `0` | Redis 密码/库 |
| `CAMPUS_RABBIT_HOST` / `CAMPUS_RABBIT_PORT` | `localhost` / `5672` | RabbitMQ |
| `CAMPUS_RABBIT_USERNAME` / `CAMPUS_RABBIT_PASSWORD` | `guest` / `guest` | RabbitMQ 账号 |
| `CAMPUS_RABBIT_VHOST` | `/` | 虚拟主机 |
| `APP_MQ_ENABLED` | `true` | 关闭后不启用 MQ 生产者/消费者 |
| `APP_JWT_SECRET` | dev 默认值 | JWT HS256 密钥(生产必须注入,≥32 字节) |

RabbitMQ 监听采用手动确认(`spring.rabbitmq.listener.simple.acknowledge-mode: manual`),
消费失败由 `NotificationConsumer` 抛 `AmqpRejectAndDontRequeueException` 进入死信队列。

## MQ 声明(RabbitConfig)

- bean 名:`rabbitConfig`。
- 交换机:`campus.exchange.order`(direct)、`campus.exchange.notify`(topic)、`campus.exchange.dlx`(direct)。
- 队列:`queue.order.events`、四个通知队列 + 两个 DLQ;主队列挂 `x-dead-letter-exchange`。
- 四个通知 Queue bean(含同名 public 字段,供 `NotificationConsumer` 的
  SpEL `#{rabbitConfig.queueNotifyOrder.name}` 等引用):
  `queueNotifyOrder` / `queueNotifyPayment` / `queueNotifyDelivery` / `queueNotifySystem`。

## MyBatis-Plus 配置

- `MybatisPlusConfig`:分页插件(`PaginationInnerInterceptor`,MySQL)+ 乐观锁插件。
- `MyMetaObjectHandler`:insert 填充 `createdBy/updatedBy/createdAt/updatedAt`,
  update 填充 `updatedBy/updatedAt`;操作人取自 `UserContext.uid()`,无上下文时兜底 `0`。
- 逻辑删除统一 `deleted` 字段(0/1),见 `application.yml`。

## 对 campus-service 的依赖说明(重要)

campus-web 的 Controller 已按 docs/api.md 全覆盖。其中以下接口对应的 Service 方法在
**当前 campus-service 快照中尚未实现**,Controller 按下列契约调用,待 campus-service
补齐(DAO 实体/Mapper 已存在于 schema 与 campus-dao 规划中):

| 接口 | 依赖的 Service 契约 |
|------|---------------------|
| `POST /api/orders/{orderNo}/refund` | `OrderService.applyRefund(Long userId, String orderNo, RefundReq req)` |
| `GET/POST /api/rider/tasks*` | `RiderService.tasks(Long userId, String status, PageQuery)` / `.available(Long)` / `.accept(Long, Long)` / `.pickup(Long, Long)` / `.deliver(Long, Long)` |
| `GET /api/merchant/orders`、`POST .../accept`、`POST .../ready` | `MerchantService.pageOrders(Long, String, PageQuery)` / `.acceptOrder(Long, String)` / `.readyOrder(Long, String)` |
| `GET /api/merchant/reviews`、`POST .../{id}/reply` | `MerchantService.listReviews(Long, PageQuery)` / `.replyReview(Long, Long, ReviewReplyReq)` |
| `GET /api/merchant/refunds`、`POST .../approve|reject` | `MerchantService.listRefunds(Long, PageQuery)` / `.approveRefund(Long, Long)` / `.rejectRefund(Long, Long, RefundReviewReq)` |

其余接口均已与现有 campus-service 源码逐一核对(见各 Controller 注入的方法签名)。
