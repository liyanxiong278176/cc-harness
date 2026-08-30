-- =====================================================================
-- 校园外卖系统 MySQL 8 初始化脚本 (01-schema.sql)
-- 说明: utf8mb4 / InnoDB / 逻辑删除 deleted / 乐观锁 version / 审计字段
-- 表前缀: sys_ 系统与用户, 业务表直名
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. 用户账号
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `sys_user`;
CREATE TABLE `sys_user` (
`id`            BIGINT       NOT NULL AUTO_INCREMENT,
`username`      VARCHAR(50)  NOT NULL,
`password_hash` VARCHAR(100) NOT NULL,
`phone`         VARCHAR(128) NOT NULL DEFAULT '',
`nickname`      VARCHAR(50)  NOT NULL DEFAULT '',
`avatar`        VARCHAR(255) NOT NULL DEFAULT '',
`role`          VARCHAR(20)  NOT NULL DEFAULT 'USER',
`status`        TINYINT      NOT NULL DEFAULT 1,
`version`       INT          NOT NULL DEFAULT 0,
`created_by`    BIGINT       NOT NULL DEFAULT 0,
`updated_by`    BIGINT       NOT NULL DEFAULT 0,
`created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`       TINYINT      NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_username` (`username`),
KEY `idx_phone` (`phone`)
);

-- ---------------------------------------------------------------------
-- 2. 用户收货地址
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `user_address`;
CREATE TABLE `user_address` (
`id`             BIGINT       NOT NULL AUTO_INCREMENT,
`user_id`        BIGINT       NOT NULL,
`receiver_name`  VARCHAR(50)  NOT NULL,
`receiver_phone` VARCHAR(128) NOT NULL,
`campus_zone`    VARCHAR(50)  NOT NULL,
`detail`         VARCHAR(200) NOT NULL,
`is_default`     TINYINT      NOT NULL DEFAULT 0,
`version`        INT          NOT NULL DEFAULT 0,
`created_by`     BIGINT       NOT NULL DEFAULT 0,
`updated_by`     BIGINT       NOT NULL DEFAULT 0,
`created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`        TINYINT      NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `idx_user` (`user_id`),
KEY `idx_default` (`user_id`,`is_default`)
);

-- ---------------------------------------------------------------------
-- 3. 商家
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `merchant`;
CREATE TABLE `merchant` (
`id`               BIGINT        NOT NULL AUTO_INCREMENT,
`name`             VARCHAR(100)  NOT NULL,
`logo`             VARCHAR(255)  NOT NULL DEFAULT '',
`description`      VARCHAR(500)  NOT NULL DEFAULT '',
`category`         VARCHAR(50)   NOT NULL DEFAULT '',
`campus_zone`      VARCHAR(50)   NOT NULL DEFAULT '',
`delivery_fee`     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`min_order_amount` DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`open_time`        TIME          NOT NULL DEFAULT '08:00:00',
`close_time`       TIME          NOT NULL DEFAULT '22:00:00',
`is_open`          TINYINT       NOT NULL DEFAULT 0,
`rating`           DECIMAL(3,2)  NOT NULL DEFAULT 5.00,
`rating_count`     INT           NOT NULL DEFAULT 0,
`version`          INT           NOT NULL DEFAULT 0,
`created_by`       BIGINT        NOT NULL DEFAULT 0,
`updated_by`       BIGINT        NOT NULL DEFAULT 0,
`created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`          TINYINT       NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `idx_zone` (`campus_zone`),
KEY `idx_open` (`is_open`)
);

-- 商家员工(绑定商家与账号)
DROP TABLE IF EXISTS `merchant_employee`;
CREATE TABLE `merchant_employee` (
`id`          BIGINT      NOT NULL AUTO_INCREMENT,
`merchant_id` BIGINT      NOT NULL,
`user_id`     BIGINT      NOT NULL,
`role`        VARCHAR(20) NOT NULL DEFAULT 'STAFF',
`status`      TINYINT     NOT NULL DEFAULT 1,
`version`     INT         NOT NULL DEFAULT 0,
`created_by`  BIGINT      NOT NULL DEFAULT 0,
`updated_by`  BIGINT      NOT NULL DEFAULT 0,
`created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`     TINYINT     NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_merchant_user` (`merchant_id`,`user_id`)
);

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
`updated_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`     TINYINT     NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `idx_merchant` (`merchant_id`)
);

DROP TABLE IF EXISTS `dish`;
CREATE TABLE `dish` (
`id`             BIGINT        NOT NULL AUTO_INCREMENT,
`merchant_id`    BIGINT        NOT NULL,
`category_id`    BIGINT        NOT NULL DEFAULT 0,
`sku_code`       VARCHAR(50)   NOT NULL DEFAULT '',
`name`           VARCHAR(100)  NOT NULL,
`description`    VARCHAR(500)  NOT NULL DEFAULT '',
`image`          VARCHAR(255)  NOT NULL DEFAULT '',
`price`          DECIMAL(10,2) NOT NULL,
`original_price` DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`stock`          INT           NOT NULL DEFAULT 0,
`sold_count`     INT           NOT NULL DEFAULT 0,
`status`         TINYINT       NOT NULL DEFAULT 1,
`version`        INT           NOT NULL DEFAULT 0,
`created_by`     BIGINT        NOT NULL DEFAULT 0,
`updated_by`     BIGINT        NOT NULL DEFAULT 0,
`created_at`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`        TINYINT       NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `idx_merchant_status` (`merchant_id`,`status`),
KEY `idx_category` (`category_id`)
);

-- 库存变动流水(审计)
DROP TABLE IF EXISTS `stock_change_log`;
CREATE TABLE `stock_change_log` (
`id`          BIGINT      NOT NULL AUTO_INCREMENT,
`dish_id`     BIGINT      NOT NULL,
`order_id`    BIGINT      NOT NULL DEFAULT 0,
`change_type` VARCHAR(20) NOT NULL,
`change_qty`  INT         NOT NULL,
`before_stock` INT        NOT NULL,
`after_stock` INT         NOT NULL,
`created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
PRIMARY KEY (`id`),
KEY `idx_dish` (`dish_id`),
KEY `idx_order` (`order_id`)
);

-- ---------------------------------------------------------------------
-- 5. 优惠券
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `coupon`;
CREATE TABLE `coupon` (
`id`              BIGINT        NOT NULL AUTO_INCREMENT,
`name`            VARCHAR(100)  NOT NULL,
`type`            VARCHAR(20)   NOT NULL,
`threshold_amount` DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`discount_amount` DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`discount_rate`   DECIMAL(4,3)  NOT NULL DEFAULT 1.000,
`total_count`     INT           NOT NULL DEFAULT 0,
`issued_count`    INT           NOT NULL DEFAULT 0,
`start_time`      DATETIME      NOT NULL,
`end_time`        DATETIME      NOT NULL,
`status`          TINYINT       NOT NULL DEFAULT 1,
`version`         INT           NOT NULL DEFAULT 0,
`created_by`      BIGINT        NOT NULL DEFAULT 0,
`updated_by`      BIGINT        NOT NULL DEFAULT 0,
`created_at`      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`         TINYINT       NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `idx_time` (`start_time`,`end_time`)
);

DROP TABLE IF EXISTS `user_coupon`;
CREATE TABLE `user_coupon` (
`id`           BIGINT        NOT NULL AUTO_INCREMENT,
`user_id`      BIGINT        NOT NULL,
`coupon_id`    BIGINT        NOT NULL,
`status`       VARCHAR(20)   NOT NULL DEFAULT 'UNUSED',
`used_order_id` BIGINT       NOT NULL DEFAULT 0,
`received_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`used_at`      DATETIME      NULL,
`expire_at`    DATETIME      NOT NULL,
`version`      INT           NOT NULL DEFAULT 0,
`created_by`   BIGINT        NOT NULL DEFAULT 0,
`updated_by`   BIGINT        NOT NULL DEFAULT 0,
`created_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`      TINYINT       NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_user_coupon` (`user_id`,`coupon_id`),
KEY `idx_status` (`user_id`,`status`)
);

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
`updated_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`     TINYINT NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_user_dish` (`user_id`,`dish_id`),
KEY `idx_merchant` (`merchant_id`)
);

-- ---------------------------------------------------------------------
-- 7. 订单
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `order_info`;
CREATE TABLE `order_info` (
`id`               BIGINT        NOT NULL AUTO_INCREMENT,
`order_no`         VARCHAR(32)   NOT NULL,
`user_id`          BIGINT        NOT NULL,
`merchant_id`      BIGINT        NOT NULL,
`address_id`       BIGINT        NOT NULL,
`address_snapshot` VARCHAR(500)  NOT NULL,
`coupon_id`        BIGINT        NOT NULL DEFAULT 0,
`coupon_snapshot`  VARCHAR(500)  NOT NULL DEFAULT '',
`total_amount`     DECIMAL(10,2) NOT NULL,
`discount_amount`  DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`delivery_fee`     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
`pay_amount`       DECIMAL(10,2) NOT NULL,
`status`           VARCHAR(30)   NOT NULL DEFAULT 'CREATED'
`remark`           VARCHAR(200)  NOT NULL DEFAULT '',
`cancel_reason`    VARCHAR(200)  NOT NULL DEFAULT '',
`cancel_time`      DATETIME      NULL,
`pay_time`         DATETIME      NULL,
`pay_trade_no`     VARCHAR(64)   NOT NULL DEFAULT '',
`pay_channel`      VARCHAR(20)   NOT NULL DEFAULT 'MOCK',
`rider_id`         BIGINT        NOT NULL DEFAULT 0,
`accept_time`      DATETIME      NULL,
`delivered_time`   DATETIME      NULL,
`completed_time`   DATETIME      NULL,
`version`          INT           NOT NULL DEFAULT 0,
`created_by`       BIGINT        NOT NULL DEFAULT 0,
`updated_by`       BIGINT        NOT NULL DEFAULT 0,
`created_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`          TINYINT       NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_order_no` (`order_no`),
KEY `idx_user` (`user_id`),
KEY `idx_merchant_status` (`merchant_id`,`status`),
KEY `idx_rider` (`rider_id`)
);

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
);

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
`trade_no`    VARCHAR(64)  NOT NULL,
`amount`      DECIMAL(10,2) NOT NULL,
`status`      VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
`raw_callback` TEXT        NULL,
`paid_at`     DATETIME     NULL,
`version`     INT          NOT NULL DEFAULT 0,
`created_by`  BIGINT       NOT NULL DEFAULT 0,
`updated_by`  BIGINT       NOT NULL DEFAULT 0,
`created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`     TINYINT      NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_trade_no` (`trade_no`),
KEY `idx_order` (`order_id`),
KEY `idx_user` (`user_id`)
);

DROP TABLE IF EXISTS `refund_record`;
CREATE TABLE `refund_record` (
`id`           BIGINT        NOT NULL AUTO_INCREMENT,
`order_id`     BIGINT        NOT NULL,
`order_no`     VARCHAR(32)   NOT NULL,
`user_id`      BIGINT        NOT NULL,
`reason`       VARCHAR(200)  NOT NULL,
`amount`       DECIMAL(10,2) NOT NULL,
`status`       VARCHAR(20)   NOT NULL DEFAULT 'PENDING',
`reviewer_id`  BIGINT        NOT NULL DEFAULT 0,
`reject_reason` VARCHAR(200) NOT NULL DEFAULT '',
`version`      INT           NOT NULL DEFAULT 0,
`created_by`   BIGINT        NOT NULL DEFAULT 0,
`updated_by`   BIGINT        NOT NULL DEFAULT 0,
`created_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`      TINYINT       NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `idx_order` (`order_id`)
);

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
`accept_time`    DATETIME    NULL,
`picked_time`    DATETIME    NULL,
`delivered_time` DATETIME    NULL,
`version`        INT         NOT NULL DEFAULT 0,
`created_by`     BIGINT      NOT NULL DEFAULT 0,
`updated_by`     BIGINT      NOT NULL DEFAULT 0,
`created_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`        TINYINT     NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_order` (`order_id`),
KEY `idx_rider_status` (`rider_id`,`status`)
);

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
`rating`         TINYINT      NOT NULL,
`content`        VARCHAR(500) NOT NULL DEFAULT '',
`images`         VARCHAR(500) NOT NULL DEFAULT '',
`reply`          VARCHAR(500) NOT NULL DEFAULT '',
`merchant_replied_at` DATETIME NULL,
`version`        INT          NOT NULL DEFAULT 0,
`created_by`     BIGINT       NOT NULL DEFAULT 0,
`updated_by`     BIGINT       NOT NULL DEFAULT 0,
`created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`        TINYINT      NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_order` (`order_id`),
KEY `idx_merchant` (`merchant_id`)
);

-- ---------------------------------------------------------------------
-- 11. 站内通知
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `notification`;
CREATE TABLE `notification` (
`id`         BIGINT      NOT NULL AUTO_INCREMENT,
`user_id`    BIGINT      NOT NULL,
`type`       VARCHAR(20) NOT NULL,
`title`      VARCHAR(100) NOT NULL,
`content`    VARCHAR(500) NOT NULL DEFAULT '',
`biz_type`   VARCHAR(30) NOT NULL DEFAULT '',
`biz_id`     VARCHAR(64) NOT NULL DEFAULT '',
`is_read`    TINYINT     NOT NULL DEFAULT 0,
`read_at`    DATETIME    NULL,
`version`    INT         NOT NULL DEFAULT 0,
`created_by` BIGINT      NOT NULL DEFAULT 0,
`updated_by` BIGINT      NOT NULL DEFAULT 0,
`created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
`deleted`    TINYINT     NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_dedup` (`user_id`,`biz_type`,`biz_id`),
KEY `idx_user_read` (`user_id`,`is_read`)
);

-- ---------------------------------------------------------------------
-- 12. 操作日志
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `operation_log`;
CREATE TABLE `operation_log` (
`id`              BIGINT       NOT NULL AUTO_INCREMENT,
`user_id`         BIGINT       NOT NULL DEFAULT 0,
`username`        VARCHAR(50)  NOT NULL DEFAULT '',
`operation`       VARCHAR(100) NOT NULL,
`module`          VARCHAR(50)  NOT NULL DEFAULT '',
`method`          VARCHAR(200) NOT NULL DEFAULT '',
`uri`             VARCHAR(200) NOT NULL DEFAULT '',
`ip`              VARCHAR(64)  NOT NULL DEFAULT '',
`params_snapshot` TEXT         NULL,
`result_snapshot` TEXT         NULL,
`success`         TINYINT      NOT NULL DEFAULT 1,
`error_msg`       VARCHAR(500) NOT NULL DEFAULT '',
`cost_ms`         BIGINT       NOT NULL DEFAULT 0,
`created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
PRIMARY KEY (`id`),
KEY `idx_user_time` (`user_id`,`created_at`),
KEY `idx_module` (`module`)
);

-- ---------------------------------------------------------------------
-- 13. 本地消息表(MQ 可靠投递 outbox)
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS `mq_message`;
CREATE TABLE `mq_message` (
`id`             BIGINT       NOT NULL AUTO_INCREMENT,
`msg_id`         VARCHAR(64)  NOT NULL,
`exchange`       VARCHAR(100) NOT NULL,
`routing_key`    VARCHAR(100) NOT NULL,
`payload`        TEXT         NOT NULL,
`status`         VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
`retry_count`    INT          NOT NULL DEFAULT 0,
`next_retry_time` DATETIME    NULL,
`last_error`     VARCHAR(500) NOT NULL DEFAULT '',
`created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
`updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
PRIMARY KEY (`id`),
UNIQUE KEY `uk_msg_id` (`msg_id`),
KEY `idx_status` (`status`,`next_retry_time`)
);

