-- =====================================================================
-- 校园外卖系统种子数据 (02-seed.sql)
-- 说明:
--  1. sys_user 密码为占位哈希 $2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE,
--     应用启动时 DataInitializer 会将演示账号密码重置为 BCrypt("123456")
--     (仅在哈希为占位符时覆盖,幂等,见 campus-web/.../config/DataInitializer.java)
--  2. 商家/菜品/优惠券/骑手为演示数据,便于端到端体验。
-- =====================================================================
SET NAMES utf8mb4;

-- ---------------- 用户账号 ----------------
INSERT INTO `sys_user` (`username`,`password_hash`,`phone`,`nickname`,`role`,`status`) VALUES
('admin',  '$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE', 'E_', '系统管理员', 'ADMIN',   1),
('zhangsan','$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE','E_', '张三',      'USER',    1),
('lisi',   '$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE', 'E_', '李四',      'USER',    1),
('m_hanbao','$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE','E_','汉堡店长',  'MERCHANT', 1),
('m_chuan','$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE', 'E_', '川菜店主',  'MERCHANT', 1),
('rider1', '$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE', 'E_', '骑手小王',  'RIDER',    1),
('rider2', '$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE', 'E_', '骑手小李',  'RIDER',    1);

-- ---------------- 地址 ----------------
INSERT INTO `user_address` (`user_id`,`receiver_name`,`receiver_phone`,`campus_zone`,`detail`,`is_default`) VALUES
(2,'张三','E_','东区','3号宿舍楼 501',1),
(2,'张三','E_','东区','图书馆一层自习区',0),
(3,'李四','E_','西区','7号宿舍楼 302',1);

-- ---------------- 商家 ----------------
INSERT INTO `merchant` (`id`,`name`,`logo`,`description`,`category`,`campus_zone`,`delivery_fee`,`min_order_amount`,`open_time`,`close_time`,`is_open`,`rating`,`rating_count`) VALUES
(1,'快乐汉堡','','现做现卖的美式汉堡','汉堡','东区',3.00,15.00,'08:00:00','22:00:00',1,4.80,120),
(2,'川味小馆','','地道川菜,麻辣鲜香','简餐','西区',2.00,20.00,'09:00:00','21:30:00',1,4.60,86),
(3,'奶茶工坊','','鲜果现萃,好喝不贵','奶茶','东区',1.00,10.00,'10:00:00','23:00:00',1,4.90,203);

INSERT INTO `merchant_employee` (`merchant_id`,`user_id`,`role`) VALUES
(1,4,'OWNER'),(2,5,'OWNER');

-- ---------------- 菜品分类 ----------------
INSERT INTO `dish_category` (`id`,`merchant_id`,`name`,`sort_order`) VALUES
(1,1,'汉堡',1),(2,1,'小食',2),(3,1,'饮品',3),
(4,2,'热菜',1),(5,2,'凉菜',2),
(6,3,'奶茶',1),(7,3,'果茶',2);

-- ---------------- 菜品(SKU) ----------------
INSERT INTO `dish` (`id`,`merchant_id`,`category_id`,`sku_code`,`name`,`description`,`price`,`original_price`,`stock`,`status`) VALUES
(101,1,1,'HB-001','经典牛肉堡','双层牛肉,新鲜蔬菜',25.00,28.00,100,1),
(102,1,1,'HB-002','芝士鸡腿堡','香脆鸡腿排+芝士',22.00,24.00,80,1),
(103,1,2,'XS-001','黄金薯条','大份现炸',12.00,0.00,200,1),
(104,1,3,'DR-001','可乐(中杯)','冰爽可乐',8.00,0.00,300,1),
(201,2,4,'CD-001','麻婆豆腐','麻辣下饭',18.00,20.00,60,1),
(202,2,4,'CD-002','宫保鸡丁','酸甜微辣',26.00,28.00,50,1),
(203,2,4,'CD-003','水煮鱼','鲜嫩鱼片',38.00,42.00,30,1),
(204,2,5,'LC-001','凉拌黄瓜','清爽解腻',10.00,0.00,100,1),
(301,3,6,'NA-001','珍珠奶茶','经典原味',12.00,14.00,500,1),
(302,3,6,'NA-002','椰奶啵啵','椰香浓郁',14.00,0.00,400,1),
(303,3,7,'FR-001','满杯百香果','维C满满',16.00,18.00,350,1),
(304,3,7,'FR-002','橙子冰茶','鲜橙现切',15.00,0.00,300,1);

-- ---------------- 优惠券 ----------------
INSERT INTO `coupon` (`id`,`name`,`type`,`threshold_amount`,`discount_amount`,`discount_rate`,`total_count`,`issued_count`,`start_time`,`end_time`,`status`) VALUES
(1,'新人满30减5','FULL_REDUCTION',30.00,5.00,1.000,1000,0,'2025-01-01 00:00:00','2026-12-31 23:59:59',1),
(2,'满50减8','FULL_REDUCTION',50.00,8.00,1.000,1000,0,'2025-01-01 00:00:00','2026-12-31 23:59:59',1),
(3,'全场9折券','DISCOUNT',0.00,0.00,0.900,500,0,'2025-01-01 00:00:00','2026-12-31 23:59:59',1);

-- 演示用户各领一张
INSERT INTO `user_coupon` (`user_id`,`coupon_id`,`expire_at`) VALUES
(2,1,'2026-12-31 23:59:59'),
(2,2,'2026-12-31 23:59:59'),
(3,3,'2026-12-31 23:59:59');

-- ---------------- 骑手 ----------------
-- 骑手即 sys_user 中 role=RIDER 的账号,配送任务通过 MockRiderDispatcher 分配。
