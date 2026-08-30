# campus-dao

MyBatis-Plus 数据访问层(零业务逻辑)。提供 19 张业务表的实体(Entity)与 Mapper,
并包含库存防超卖、优惠券防超发/核销、通知已读等条件更新自定义 SQL。

## 模块结构

```
campus-dao/
├── pom.xml
└── src/main/java/com/campus/dao/
    ├── entity/   实体类(19 张表,见下表)
    └── mapper/   Mapper 接口(继承 BaseMapper<T>,含自定义 @Update/@Select)
```

- 依赖: `campus-common`(${project.version}) + `mybatis-plus-spring-boot3-starter`(版本由父 pom
  dependencyManagement 管理,3.5.5)。
- 包名: 实体 `com.campus.dao.entity`,Mapper `com.campus.dao.mapper`。

## 实体清单(19 张表,与 db/init/01-schema.sql 一一对应)

| 表名              | 实体类         | 说明                     |
|-------------------|----------------|--------------------------|
| sys_user          | SysUser        | 用户账号                 |
| user_address      | UserAddress    | 用户收货地址             |
| merchant          | Merchant       | 商家                     |
| merchant_employee | MerchantEmployee | 商家员工(商家与账号绑定) |
| dish_category     | DishCategory   | 菜品分类                 |
| dish              | Dish           | 菜品(SKU)                |
| stock_change_log  | StockChangeLog | 库存变动流水(审计)       |
| coupon            | Coupon         | 优惠券模板               |
| user_coupon       | UserCoupon     | 用户优惠券               |
| cart              | Cart           | 购物车                   |
| order_info        | OrderInfo      | 订单主表                 |
| order_item        | OrderItem      | 订单明细(快照)           |
| payment_record    | PaymentRecord  | 支付流水(回调幂等锚点)   |
| refund_record     | RefundRecord   | 退款申请                 |
| delivery_task     | DeliveryTask   | 配送任务                 |
| review            | Review         | 订单评价                 |
| notification      | Notification   | 站内通知                 |
| operation_log     | OperationLog   | 操作日志                 |
| mq_message        | MqMessage      | 本地消息表(MQ outbox)    |

### 基类与字段约定

- `BaseEntity`: 抽象基类,包含
  - `id`:`@TableId(type = IdType.AUTO)`(BIGINT 自增,Long);
  - `version`:`@Version` 乐观锁(Long,updateById 时由 OptimisticLocker 拦截器校验并自增);
  - `deleted`:`@TableLogic` 逻辑删除(Integer,1=已删 0=未删);
  - 审计字段:`created_by`/`updated_by`(Long)、`created_at`/`updated_at`(LocalDateTime)。
- **不继承 BaseEntity 的 4 张表**(无 deleted/version/审计字段):
  `stock_change_log`、`order_item`、`operation_log`、`mq_message`(各自独立实现,仅含自身列)。
- 类型映射规则: `DECIMAL → BigDecimal`、`DATETIME → LocalDateTime`、`TIME → String`、
  `TINYINT/INT → Integer`、`BIGINT → Long`、`VARCHAR/TEXT → String`。
- 状态字段一律 `String` 或 `Integer`,**不使用 enum**(如 order_info.status、
  user_coupon.status、coupon.type、notification.type 均为 String;sys_user.status 等 TINYINT 为 Integer)。
- 特例: `SysUser.lastLoginAt` 为 campus-service 使用但 schema 无此列,
  已标注 `@TableField(exist = false)`(仅内存使用,不参与持久化,避免运行时 SQL 报错)。

## 自定义 SQL 方法说明

### DishMapper(库存防超卖)
- `int deductStock(@Param("id") Long id, @Param("qty") int qty, @Param("version") Long version)`
  ```sql
  UPDATE dish SET stock = stock - #{qty}, sold_count = sold_count + #{qty}, version = version + 1
  WHERE id = #{id} AND stock >= #{qty} AND version = #{version} AND deleted = 0
  ```
  条件更新: 库存充足 + 乐观锁版本匹配才扣减;返回 0 表示库存不足或并发冲突。
- `int rollbackStock(@Param("id") Long id, @Param("qty") int qty)`
  ```sql
  UPDATE dish SET stock = stock + #{qty}, sold_count = sold_count - #{qty}, version = version + 1
  WHERE id = #{id} AND sold_count >= #{qty} AND deleted = 0
  ```
  取消/退款时回滚库存。

### UserCouponMapper(优惠券核销/退还)
- `int markUsed(@Param("id") Long id, @Param("orderId") Long orderId, @Param("version") Long version)`
  ```sql
  UPDATE user_coupon SET status = 'USED', used_order_id = #{orderId}, used_at = NOW(), version = version + 1
  WHERE id = #{id} AND status = 'UNUSED' AND version = #{version} AND deleted = 0
  ```
  条件更新 UNUSED -> USED,防并发重复核销。
- `int release(@Param("orderId") Long orderId, @Param("userId") Long userId)`
  ```sql
  UPDATE user_coupon SET status = 'UNUSED', used_order_id = 0, used_at = NULL, version = version + 1
  WHERE user_id = #{userId} AND used_order_id = #{orderId} AND status = 'USED' AND deleted = 0
  ```
  退款/取消时退还券(USED -> UNUSED)。

### CouponMapper(领券防超发)
- `int incrementIssued(@Param("id") Long id, @Param("delta") int delta, @Param("totalCount") int totalCount)`
  ```sql
  UPDATE coupon SET issued_count = issued_count + #{delta}, version = version + 1
  WHERE id = #{id} AND issued_count + #{delta} <= #{totalCount} AND deleted = 0
  ```
  已发行量 + delta 不超过发行总量才更新;返回 0 表示已超发。

### NotificationMapper(已读)
- `int markAllRead(@Param("userId") Long userId)`
  ```sql
  UPDATE notification SET is_read = 1, read_at = NOW()
  WHERE user_id = #{userId} AND is_read = 0 AND deleted = 0
  ```

### 其它自定义方法
- `UserAddressMapper.clearDefault(@Param("userId") Long userId)`:
  `UPDATE user_address SET is_default = 0 WHERE user_id = #{userId} AND is_default = 1 AND deleted = 0`。
- `OrderInfoMapper.sumPayAmount(@Param("merchantId") Long merchantId,
  @Param("from") LocalDateTime from, @Param("to") LocalDateTime to)`:
  按支付时间口径汇总某商家区间内实付金额(排除 CREATED/CANCELLED,返回 BigDecimal)。

## 约定

- 所有 Mapper 标注 `@Mapper` 并继承 `BaseMapper<T>`,由 MyBatis-Plus 自动提供
  CRUD/分页/条件构造能力;自定义 SQL 一律使用参数化 `#{}`,禁止 `${}`。
- 分页插件 / 乐观锁拦截器等 MyBatis-Plus 配置由应用层(campus-web)装配。
