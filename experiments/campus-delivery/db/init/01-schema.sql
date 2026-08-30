-- =====================================================================
-- 校园外卖系统 MySQL 8 初始化脚本 (01-schema.sql)
-- 说明: utf8mb4 / InnoDB / 逻辑删除 deleted / 乐观锁 version / 审计字段
-- 表前缀: sys_ 系统与用户, 业务表直名
-- =====================================================================
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------
-- 1. 用户账号
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `sys_user`;
CREATE TABLE `sys_user` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `username`      VARCHAR(50)  NOT NULL COMMENT '登录名(唯一)',
  `password_hash` VARCHAR(100) NOT NULL COMMENT 'BCrypt 密码散列',
  `phone`         VARCHAR(128) NOT NULL DEFAULT '' COMMENT '手机号(AES 加密存储)',
  `nickname`      VARCHAR(50)  NOT NULL DEFAULT '' COMMENT '昵称',
  `avatar`        VARCHAR(255) NOT NULL DEFAULT '' COMMENT '头像',
  `role`          VARCHAR(20)  NOT NULL DEFAULT 'USER' COMMENT '角色: USER/MERCHANT/RIDER/ADMIN',
  `status`        TINYINT      NOT NULL DEFAULT 1 COMMENT '1启用 0禁用',
  `version`       INT          NOT NULL DEFAULT 0 COMMENT '乐观锁版本',
  `created_by`    BIGINT       NOT NULL DEFAULT 0,
  `updated_by`    BIGINT       NOT NULL DEFAULT 0,
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`       TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除 0否1是',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_username` (`username`),
  KEY `idx_phone` (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户账号';

-- ---------------------------------------------------------------------
-- 2. 用户收货地址
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `user_address`;
CREATE TABLE `user_address` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `user_id`        BIGINT       NOT NULL,
  `receiver_name`  VARCHAR(50)  NOT NULL,
  `receiver_phone` VARCHAR(128) NOT NULL COMMENT '手机号(AES 加密)',
  `campus_zone`    VARCHAR(50)  NOT NULL COMMENT '校区/楼栋区',
  `detail`         VARCHAR(200) NOT NULL COMMENT '详细地址',
  `is_default`     TINYINT      NOT NULL DEFAULT 0,
  `version`        INT          NOT NULL DEFAULT 0,
  `created_by`     BIGINT       NOT NULL DEFAULT 0,
  `updated_by`     BIGINT       NOT NULL DEFAULT 0,
  `created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`        TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`),
  KEY `idx_default` (`user_id`,`is_default`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户收货地址';

-- ---------------------------------------------------------------------
-- 3. 商家
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `merchant`;
CREATE TABLE `merchant` (
  `id`               BIGINT        NOT NULL AUTO_INCREMENT,
  `name`             VARCHAR(100)  NOT NULL,
  `logo`             VARCHAR(255)  NOT NULL DEFAULT '',
  `description`      VARCHAR(500)  NOT NULL DEFAULT '',
  `category`         VARCHAR(50)   NOT NULL DEFAULT '' COMMENT '简餐/奶茶/汉堡等',
  `campus_zone`      VARCHAR(50)   NOT NULL DEFAULT '' COMMENT '覆盖校区',
  `delivery_fee`     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  `min_order_amount` DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '起送价',
  `open_time`        TIME          NOT NULL DEFAULT '08:00:00',
  `close_time`       TIME          NOT NULL DEFAULT '22:00:00',
  `is_open`          TINYINT       NOT NULL DEFAULT 0 COMMENT '营业状态 1营业 0打烊',
  `rating`           DECIMAL(3,2)  NOT NULL DEFAULT 5.00 COMMENT '评分',
  `rating_count`     INT           NOT NULL DEFAULT 0,
  `version`          INT           NOT NULL DEFAULT 0,
  `created_by`       BIGINT        NOT NULL DEFAULT 0,
  `updated_by`       BIGINT        NOT NULL DEFAULT 0,
  `created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`          TINYINT       NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_zone` (`campus_zone`),
  KEY `idx_open` (`is_open`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商家';

-- 商家员工(绑定商家与账号)
DROP TABLE IF EXISTS `merchant_employee`;
CREATE TABLE `merchant_employee` (
  `id`          BIGINT      NOT NULL AUTO_INCREMENT,
  `merchant_id` BIGINT      NOT NULL,
  `user_id`     BIGINT      NOT NULL,
  `role`        VARCHAR(20) NOT NULL DEFAULT 'STAFF' COMMENT 'OWNER/STAFF',
  `status`      TINYINT     NOT NULL DEFAULT 1,
  `version`     INT         NOT NULL DEFAULT 0,
  `created_by`  BIGINT      NOT NULL DEFAULT 0,
  `updated_by`  BIGINT      NOT NULL DEFAULT 0,
  `created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`     TINYINT     NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_merchant_user` (`merchant_id`,`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商家员工';

-- ---------------------------------------------------------------------
-- 4. 菜品分类 / 菜品(SKU)
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `dish_category`;
CREATE TABLE `dish_category` (
  `id`          BIGINT      NOT NULL AUTO_INCREMENT,
  `merchant_id` BIGINT      NOT NULL,
  `name`        VARCHAR(50) NOT NULL,
  `sort_order`  INT         NOT NULL DEFAULT 0,
  `status`      TINYINT     NOT NULL DEFAULT 1,
  `version`     INT         NOT NULL DEFAULT 0,
  `created_by`  BIGINT      NOT NULL DEFAULT 0,
  `updated_by`  BIGINT      NOT NULL DEFAULT 0,
  `created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`     TINYINT     NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_merchant` (`merchant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='菜品分类';

DROP TABLE IF EXISTS `dish`;
CREATE TABLE `dish` (
  `id`             BIGINT        NOT NULL AUTO_INCREMENT,
  `merchant_id`    BIGINT        NOT NULL,
  `category_id`    BIGINT        NOT NULL DEFAULT 0,
  `sku_code`       VARCHAR(50)   NOT NULL DEFAULT '' COMMENT 'SKU 编码',
  `name`           VARCHAR(100)  NOT NULL,
  `description`    VARCHAR(500)  NOT NULL DEFAULT '',
  `image`          VARCHAR(255)  NOT NULL DEFAULT '',
  `price`          DECIMAL(10,2) NOT NULL,
  `original_price` DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  `stock`          INT           NOT NULL DEFAULT 0 COMMENT '可售库存',
  `sold_count`     INT           NOT NULL DEFAULT 0,
  `status`         TINYINT       NOT NULL DEFAULT 1 COMMENT '1上架 0下架',
  `version`        INT           NOT NULL DEFAULT 0 COMMENT '乐观锁(防超卖)',
  `created_by`     BIGINT        NOT NULL DEFAULT 0,
  `updated_by`     BIGINT        NOT NULL DEFAULT 0,
  `created_at`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`        TINYINT       NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_merchant_status` (`merchant_id`,`status`),
  KEY `idx_category` (`category_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='菜品(SKU)';

-- 库存变动流水(审计)
DROP TABLE IF EXISTS `stock_change_log`;
CREATE TABLE `stock_change_log` (
  `id`          BIGINT      NOT NULL AUTO_INCREMENT,
  `dish_id`     BIGINT      NOT NULL,
  `order_id`    BIGINT      NOT NULL DEFAULT 0,
  `change_type` VARCHAR(20) NOT NULL COMMENT 'DEDUCT/ROLLBACK/RESTOCK',
  `change_qty`  INT         NOT NULL,
  `before_stock` INT        NOT NULL,
  `after_stock` INT         NOT NULL,
  `created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_dish` (`dish_id`),
  KEY `idx_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存变动流水';

-- ---------------------------------------------------------------------
-- 5. 优惠券
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `coupon`;
CREATE TABLE `coupon` (
  `id`              BIGINT        NOT NULL AUTO_INCREMENT,
  `name`            VARCHAR(100)  NOT NULL,
  `type`            VARCHAR(20)   NOT NULL COMMENT 'FULL_REDUCTION满减 / DISCOUNT折扣',
  `threshold_amount` DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '满额门槛',
  `discount_amount` DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '满减金额(type=FULL_REDUCTION)',
  `discount_rate`   DECIMAL(4,3)  NOT NULL DEFAULT 1.000 COMMENT '折扣率(type=DISCOUNT)',
  `total_count`     INT           NOT NULL DEFAULT 0 COMMENT '发行总量',
  `issued_count`    INT           NOT NULL DEFAULT 0,
  `start_time`      DATETIME      NOT NULL,
  `end_time`        DATETIME      NOT NULL,
  `status`          TINYINT       NOT NULL DEFAULT 1,
  `version`         INT           NOT NULL DEFAULT 0,
  `created_by`      BIGINT        NOT NULL DEFAULT 0,
  `updated_by`      BIGINT        NOT NULL DEFAULT 0,
  `created_at`      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`         TINYINT       NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_time` (`start_time`,`end_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='优惠券模板';

DROP TABLE IF EXISTS `user_coupon`;
CREATE TABLE `user_coupon` (
  `id`           BIGINT        NOT NULL AUTO_INCREMENT,
  `user_id`      BIGINT        NOT NULL,
  `coupon_id`    BIGINT        NOT NULL,
  `status`       VARCHAR(20)   NOT NULL DEFAULT 'UNUSED' COMMENT 'UNUSED/USED/EXPIRED',
  `used_order_id` BIGINT       NOT NULL DEFAULT 0,
  `received_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `used_at`      DATETIME      NULL,
  `expire_at`    DATETIME      NOT NULL,
  `version`      INT           NOT NULL DEFAULT 0,
  `created_by`   BIGINT        NOT NULL DEFAULT 0,
  `updated_by`   BIGINT        NOT NULL DEFAULT 0,
  `created_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`      TINYINT       NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_coupon` (`user_id`,`coupon_id`),
  KEY `idx_status` (`user_id`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户优惠券';

-- ---------------------------------------------------------------------
-- 6. 购物车
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `cart`;
CREATE TABLE `cart` (
  `id`          BIGINT NOT NULL AUTO_INCREMENT,
  `user_id`     BIGINT NOT NULL,
  `merchant_id` BIGINT NOT NULL,
  `dish_id`     BIGINT NOT NULL,
  `quantity`    INT    NOT NULL DEFAULT 1,
  `checked`     TINYINT NOT NULL DEFAULT 1,
  `version`     INT    NOT NULL DEFAULT 0,
  `created_by`  BIGINT NOT NULL DEFAULT 0,
  `updated_by`  BIGINT NOT NULL DEFAULT 0,
  `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`     TINYINT NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_dish` (`user_id`,`dish_id`),
  KEY `idx_merchant` (`merchant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='购物车';

-- ---------------------------------------------------------------------
-- 7. 订单
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `order_info`;
CREATE TABLE `order_info` (
  `id`               BIGINT        NOT NULL AUTO_INCREMENT,
  `order_no`         VARCHAR(32)   NOT NULL COMMENT '业务订单号',
  `user_id`          BIGINT        NOT NULL,
  `merchant_id`      BIGINT        NOT NULL,
  `address_id`       BIGINT        NOT NULL,
  `address_snapshot` VARCHAR(500)  NOT NULL COMMENT '下单时地址快照(JSON,手机号加密)',
  `coupon_id`        BIGINT        NOT NULL DEFAULT 0,
  `coupon_snapshot`  VARCHAR(500)  NOT NULL DEFAULT '' COMMENT '券快照(JSON)',
  `total_amount`     DECIMAL(10,2) NOT NULL COMMENT '商品总额',
  `discount_amount`  DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '优惠总额',
  `delivery_fee`     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  `pay_amount`       DECIMAL(10,2) NOT NULL COMMENT '实付=total-discount+delivery',
  `status`           VARCHAR(30)   NOT NULL DEFAULT 'CREATED'
                     COMMENT 'CREATED/PAID/PREPARING/DELIVERING/COMPLETED/CANCELLED/REFUNDING/REFUNDED',
  `remark`           VARCHAR(200)  NOT NULL DEFAULT '',
  `cancel_reason`    VARCHAR(200)  NOT NULL DEFAULT '',
  `cancel_time`      DATETIME      NULL,
  `pay_time`         DATETIME      NULL,
  `pay_trade_no`     VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '支付平台交易号',
  `pay_channel`      VARCHAR(20)   NOT NULL DEFAULT 'MOCK',
  `rider_id`         BIGINT        NOT NULL DEFAULT 0,
  `accept_time`      DATETIME      NULL,
  `delivered_time`   DATETIME      NULL,
  `completed_time`   DATETIME      NULL,
  `version`          INT           NOT NULL DEFAULT 0 COMMENT '乐观锁(状态流转并发保护)',
  `created_by`       BIGINT        NOT NULL DEFAULT 0,
  `updated_by`       BIGINT        NOT NULL DEFAULT 0,
  `created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`          TINYINT       NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  KEY `idx_user` (`user_id`),
  KEY `idx_merchant_status` (`merchant_id`,`status`),
  KEY `idx_rider` (`rider_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单主表';

DROP TABLE IF EXISTS `order_item`;
CREATE TABLE `order_item` (
  `id`                 BIGINT        NOT NULL AUTO_INCREMENT,
  `order_id`           BIGINT        NOT NULL,
  `dish_id`            BIGINT        NOT NULL,
  `dish_name_snapshot` VARCHAR(100)  NOT NULL,
  `dish_price_snapshot` DECIMAL(10,2) NOT NULL,
  `quantity`           INT           NOT NULL,
  `subtotal`           DECIMAL(10,2) NOT NULL,
  `created_at`         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_dish` (`order_id`,`dish_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单明细(快照)';

-- ---------------------------------------------------------------------
-- 8. 支付 / 退款
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `payment_record`;
CREATE TABLE `payment_record` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `order_id`    BIGINT       NOT NULL,
  `order_no`    VARCHAR(32)  NOT NULL,
  `user_id`     BIGINT       NOT NULL,
  `channel`     VARCHAR(20)  NOT NULL DEFAULT 'MOCK',
  `trade_no`    VARCHAR(64)  NOT NULL COMMENT '渠道交易号(幂等唯一)',
  `amount`      DECIMAL(10,2) NOT NULL,
  `status`      VARCHAR(20)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/SUCCESS/FAILED/CLOSED',
  `raw_callback` TEXT        NULL COMMENT '回调原始报文(脱敏)',
  `paid_at`     DATETIME     NULL,
  `version`     INT          NOT NULL DEFAULT 0,
  `created_by`  BIGINT       NOT NULL DEFAULT 0,
  `updated_by`  BIGINT       NOT NULL DEFAULT 0,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`     TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_trade_no` (`trade_no`),
  KEY `idx_order` (`order_id`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='支付流水(回调幂等锚点)';

DROP TABLE IF EXISTS `refund_record`;
CREATE TABLE `refund_record` (
  `id`           BIGINT        NOT NULL AUTO_INCREMENT,
  `order_id`     BIGINT        NOT NULL,
  `order_no`     VARCHAR(32)   NOT NULL,
  `user_id`      BIGINT        NOT NULL,
  `reason`       VARCHAR(200)  NOT NULL,
  `amount`       DECIMAL(10,2) NOT NULL,
  `status`       VARCHAR(20)   NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/APPROVED/REJECTED/REFUNDED',
  `reviewer_id`  BIGINT        NOT NULL DEFAULT 0,
  `reject_reason` VARCHAR(200) NOT NULL DEFAULT '',
  `version`      INT           NOT NULL DEFAULT 0,
  `created_by`   BIGINT        NOT NULL DEFAULT 0,
  `updated_by`   BIGINT        NOT NULL DEFAULT 0,
  `created_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`      TINYINT       NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='退款申请';

-- ---------------------------------------------------------------------
-- 9. 配送任务
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `delivery_task`;
CREATE TABLE `delivery_task` (
  `id`             BIGINT      NOT NULL AUTO_INCREMENT,
  `order_id`       BIGINT      NOT NULL,
  `order_no`       VARCHAR(32) NOT NULL,
  `rider_id`       BIGINT      NOT NULL DEFAULT 0,
  `merchant_id`    BIGINT      NOT NULL DEFAULT 0,
  `user_id`        BIGINT      NOT NULL DEFAULT 0,
  `pickup_address` VARCHAR(200) NOT NULL DEFAULT '',
  `delivery_address` VARCHAR(500) NOT NULL DEFAULT '',
  `status`         VARCHAR(30) NOT NULL DEFAULT 'WAIT_ACCEPT'
                   COMMENT 'WAIT_ACCEPT/ACCEPTED/PICKED/DELIVERING/DELIVERED/CANCELLED',
  `accept_time`    DATETIME    NULL,
  `picked_time`    DATETIME    NULL,
  `delivered_time` DATETIME    NULL,
  `version`        INT         NOT NULL DEFAULT 0,
  `created_by`     BIGINT      NOT NULL DEFAULT 0,
  `updated_by`     BIGINT      NOT NULL DEFAULT 0,
  `created_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`        TINYINT     NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order` (`order_id`),
  KEY `idx_rider_status` (`rider_id`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='配送任务';

-- ---------------------------------------------------------------------
-- 10. 评价
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `review`;
CREATE TABLE `review` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `order_id`       BIGINT       NOT NULL,
  `user_id`        BIGINT       NOT NULL,
  `merchant_id`    BIGINT       NOT NULL,
  `dish_id`        BIGINT       NOT NULL DEFAULT 0,
  `rating`         TINYINT      NOT NULL COMMENT '1-5',
  `content`        VARCHAR(500) NOT NULL DEFAULT '',
  `images`         VARCHAR(500) NOT NULL DEFAULT '',
  `reply`          VARCHAR(500) NOT NULL DEFAULT '',
  `merchant_replied_at` DATETIME NULL,
  `version`        INT          NOT NULL DEFAULT 0,
  `created_by`     BIGINT       NOT NULL DEFAULT 0,
  `updated_by`     BIGINT       NOT NULL DEFAULT 0,
  `created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`        TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order` (`order_id`),
  KEY `idx_merchant` (`merchant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单评价';

-- ---------------------------------------------------------------------
-- 11. 站内通知
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `notification`;
CREATE TABLE `notification` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT,
  `user_id`    BIGINT      NOT NULL,
  `type`       VARCHAR(20) NOT NULL COMMENT 'ORDER_STATUS/PAYMENT/DELIVERY/SYSTEM',
  `title`      VARCHAR(100) NOT NULL,
  `content`    VARCHAR(500) NOT NULL DEFAULT '',
  `biz_type`   VARCHAR(30) NOT NULL DEFAULT '' COMMENT '业务类型(幂等键一部分)',
  `biz_id`     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '业务ID(幂等键一部分)',
  `is_read`    TINYINT     NOT NULL DEFAULT 0,
  `read_at`    DATETIME    NULL,
  `version`    INT         NOT NULL DEFAULT 0,
  `created_by` BIGINT      NOT NULL DEFAULT 0,
  `updated_by` BIGINT      NOT NULL DEFAULT 0,
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`    TINYINT     NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dedup` (`user_id`,`biz_type`,`biz_id`) COMMENT '消息幂等唯一键',
  KEY `idx_user_read` (`user_id`,`is_read`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='站内通知';

-- ---------------------------------------------------------------------
-- 12. 操作日志
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `operation_log`;
CREATE TABLE `operation_log` (
  `id`              BIGINT       NOT NULL AUTO_INCREMENT,
  `user_id`         BIGINT       NOT NULL DEFAULT 0,
  `username`        VARCHAR(50)  NOT NULL DEFAULT '',
  `operation`       VARCHAR(100) NOT NULL COMMENT '操作名',
  `module`          VARCHAR(50)  NOT NULL DEFAULT '',
  `method`          VARCHAR(200) NOT NULL DEFAULT '',
  `uri`             VARCHAR(200) NOT NULL DEFAULT '',
  `ip`              VARCHAR(64)  NOT NULL DEFAULT '',
  `params_snapshot` TEXT         NULL COMMENT '参数(脱敏后)',
  `result_snapshot` TEXT         NULL,
  `success`         TINYINT      NOT NULL DEFAULT 1,
  `error_msg`       VARCHAR(500) NOT NULL DEFAULT '',
  `cost_ms`         BIGINT       NOT NULL DEFAULT 0,
  `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_user_time` (`user_id`,`created_at`),
  KEY `idx_module` (`module`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作日志';

-- ---------------------------------------------------------------------
-- 13. 本地消息表(MQ 可靠投递 outbox)
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `mq_message`;
CREATE TABLE `mq_message` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `msg_id`         VARCHAR(64)  NOT NULL COMMENT '消息ID(唯一,幂等锚点)',
  `exchange`       VARCHAR(100) NOT NULL,
  `routing_key`    VARCHAR(100) NOT NULL,
  `payload`        TEXT         NOT NULL,
  `status`         VARCHAR(20)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/SENT/FAILED',
  `retry_count`    INT          NOT NULL DEFAULT 0,
  `next_retry_time` DATETIME    NULL,
  `last_error`     VARCHAR(500) NOT NULL DEFAULT '',
  `created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_msg_id` (`msg_id`),
  KEY `idx_status` (`status`,`next_retry_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MQ 可靠投递本地消息';

SET FOREIGN_KEY_CHECKS = 1;
