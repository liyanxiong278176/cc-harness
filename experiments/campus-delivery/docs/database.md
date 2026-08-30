# 数据库设计 (ER / 表设计)

> 单库单表,MySQL 8 InnoDB,utf8mb4。所有业务表含审计字段 `created_at/updated_at/created_by/updated_by`、逻辑删除 `deleted`、乐观锁 `version`。

## 1. ER 概览

```
sys_user 1 ── n user_address
sys_user 1 ── n user_coupon  n ── 1 coupon
merchant 1 ── n merchant_employee  n ── 1 sys_user(员工账号)
merchant 1 ── n dish_category 1 ── n dish
sys_user 1 ── n cart  n ── 1 dish
sys_user 1 ── n order_info 1 ── n order_item
order_info 1 ── 1 payment_record
order_info 1 ── n refund_record
order_info 1 ── 1 delivery_task  n ── 1 sys_user(rider)
order_info 1 ── n review  n ── 1 merchant
sys_user 1 ── n notification
dish 1 ── n stock_change_log
mq_message (独立 outbox)
```

## 2. 表清单(19 张)

| # | 表 | 用途 | 关键唯一键/索引 |
|---|----|------|-----------------|
| 1 | sys_user | 账号(USER/MERCHANT/RIDER/ADMIN 统一账号) | uk_username |
| 2 | user_address | 收货地址(手机号 AES) | idx_user, idx_default |
| 3 | merchant | 商家档案/营业状态/评分 | idx_zone, idx_open |
| 4 | merchant_employee | 商家员工绑定 | uk_merchant_user |
| 5 | dish_category | 菜品分类 | idx_merchant |
| 6 | dish | 菜品 SKU(价格/库存/上下架) | idx_merchant_status, idx_category |
| 7 | stock_change_log | 库存变动流水(审计) | idx_dish, idx_order |
| 8 | coupon | 优惠券模板 | idx_time |
| 9 | user_coupon | 用户领券(状态机 UNUSED/USED/EXPIRED) | uk_user_coupon |
| 10 | cart | 购物车行 | uk_user_dish, idx_merchant |
| 11 | order_info | 订单主表(地址/券/金额快照) | uk_order_no, idx_user, idx_merchant_status, idx_rider |
| 12 | order_item | 订单明细(名称/价格快照) | uk_order_dish |
| 13 | payment_record | 支付流水(回调幂等锚点) | uk_trade_no, idx_order |
| 14 | refund_record | 退款申请 | idx_order |
| 15 | delivery_task | 配送任务(骑手接单/状态) | uk_order, idx_rider_status |
| 16 | review | 订单评价 + 商家回复 | uk_order, idx_merchant |
| 17 | notification | 站内通知(消费幂等) | uk_dedup(user_id,biz_type,biz_id), idx_user_read |
| 18 | operation_log | 操作日志(审计) | idx_user_time, idx_module |
| 19 | mq_message | MQ 可靠投递 outbox | uk_msg_id, idx_status |

## 3. 关键设计要点

- **order_info**: 下单时对地址、优惠券、菜品价格做**快照**(`address_snapshot`/`coupon_snapshot`/order_item 快照),保证历史订单不随主数据变更而失真。
- **金额**: 一律 `DECIMAL(10,2)`,Java 侧 BigDecimal(见 `MoneyUtils`),禁止浮点。
- **状态字段**: VARCHAR 存状态名(CREATED/PAID/…),常量集中在 `com.campus.common.constant.Constants`,状态迁移由 `OrderStateMachine`/`DeliveryStateMachine` 显式表校验。
- **幂等锚点**: `payment_record.trade_no` 唯一、`notification.uk_dedup` 唯一、`mq_message.msg_id` 唯一。
- **防超卖**: `dish.stock` + `version` 条件更新(`WHERE stock>=? AND version=?`),见 docs/stock-and-concurrency.md。
- **逻辑删除**: MyBatis-Plus `@TableLogic` 字段 `deleted`,唯一键不受逻辑删除干扰(业务上不删行)。
- **手机号**: `phone`/`receiver_phone` 列为 AES-128-GCM 密文(Base64(iv+ct)),密钥来自环境变量 `APP_CRYPTO_KEY`。

## 4. 迁移策略

- 应用内迁移: Flyway,脚本 `campus-web/src/main/resources/db/migration/V1__init_schema.sql`、`V2__seed_data.sql`。
- 独立初始化: `db/init/01-schema.sql`、`02-seed.sql`(与迁移脚本同源,供手动/容器 entrypoint 使用)。
- 两条路径二选一,避免重复执行(见 docs/operations.md)。
