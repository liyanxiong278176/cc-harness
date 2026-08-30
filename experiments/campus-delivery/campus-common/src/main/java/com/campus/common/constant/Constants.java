package com.campus.common.constant;

/**
 * 常量集中定义,禁止魔法字符串散落。
 */
public final class Constants {

    private Constants() {
    }

    /** 用户角色 */
    public static final class UserRole {
        public static final String USER = "USER";
        public static final String MERCHANT = "MERCHANT";
        public static final String RIDER = "RIDER";
        public static final String ADMIN = "ADMIN";

        private UserRole() {
        }
    }

    /** 订单状态(状态机: CREATED→PAID→PREPARING→DELIVERING→COMPLETED;取消: CREATED→CANCELLED;退款: PAID..→REFUNDING→REFUNDED) */
    public static final class OrderStatus {
        public static final String CREATED = "CREATED";
        public static final String PAID = "PAID";
        public static final String PREPARING = "PREPARING";
        public static final String DELIVERING = "DELIVERING";
        public static final String COMPLETED = "COMPLETED";
        public static final String CANCELLED = "CANCELLED";
        public static final String REFUNDING = "REFUNDING";
        public static final String REFUNDED = "REFUNDED";

        private OrderStatus() {
        }
    }

    /** 支付记录状态 */
    public static final class PayStatus {
        public static final String PENDING = "PENDING";
        public static final String SUCCESS = "SUCCESS";
        public static final String FAILED = "FAILED";
        public static final String CLOSED = "CLOSED";

        private PayStatus() {
        }
    }

    /** 退款状态 */
    public static final class RefundStatus {
        public static final String PENDING = "PENDING";
        public static final String APPROVED = "APPROVED";
        public static final String REJECTED = "REJECTED";
        public static final String REFUNDED = "REFUNDED";

        private RefundStatus() {
        }
    }

    /** 配送任务状态 */
    public static final class DeliveryStatus {
        public static final String WAIT_ACCEPT = "WAIT_ACCEPT";
        public static final String ACCEPTED = "ACCEPTED";
        public static final String PICKED = "PICKED";
        public static final String DELIVERING = "DELIVERING";
        public static final String DELIVERED = "DELIVERED";
        public static final String CANCELLED = "CANCELLED";

        private DeliveryStatus() {
        }
    }

    /** 优惠券类型 */
    public static final class CouponType {
        public static final String FULL_REDUCTION = "FULL_REDUCTION";
        public static final String DISCOUNT = "DISCOUNT";

        private CouponType() {
        }
    }

    /** 用户券状态 */
    public static final class UserCouponStatus {
        public static final String UNUSED = "UNUSED";
        public static final String USED = "USED";
        public static final String EXPIRED = "EXPIRED";

        private UserCouponStatus() {
        }
    }

    /** 通知类型 */
    public static final class NotificationType {
        public static final String ORDER_STATUS = "ORDER_STATUS";
        public static final String PAYMENT = "PAYMENT";
        public static final String DELIVERY = "DELIVERY";
        public static final String SYSTEM = "SYSTEM";

        private NotificationType() {
        }
    }

    /** 库存变动类型 */
    public static final class StockChangeType {
        public static final String DEDUCT = "DEDUCT";
        public static final String ROLLBACK = "ROLLBACK";
        public static final String RESTOCK = "RESTOCK";

        private StockChangeType() {
        }
    }

    /** 本地消息状态 */
    public static final class MqMsgStatus {
        public static final String PENDING = "PENDING";
        public static final String SENT = "SENT";
        public static final String FAILED = "FAILED";

        private MqMsgStatus() {
        }
    }

    /** MQ 交换机/队列/路由键 */
    public static final class Mq {
        public static final String EXCHANGE_ORDER = "campus.exchange.order";
        public static final String EXCHANGE_NOTIFY = "campus.exchange.notify";
        public static final String EXCHANGE_DLX = "campus.exchange.dlx";

        public static final String QUEUE_ORDER_EVENTS = "queue.order.events";
        public static final String QUEUE_NOTIFY_ORDER = "queue.notify.order";
        public static final String QUEUE_NOTIFY_PAYMENT = "queue.notify.payment";
        public static final String QUEUE_NOTIFY_DELIVERY = "queue.notify.delivery";
        public static final String QUEUE_NOTIFY_SYSTEM = "queue.notify.system";
        public static final String QUEUE_ORDER_DLQ = "queue.order.dlq";
        public static final String QUEUE_NOTIFY_DLQ = "queue.notify.dlq";

        public static final String RK_ORDER_CREATED = "order.created";
        public static final String RK_ORDER_PAID = "order.paid";
        public static final String RK_ORDER_STATUS = "order.status.changed";
        public static final String RK_DELIVERY = "delivery.status.changed";
        public static final String RK_REFUND = "refund.result";

        private Mq() {
        }
    }

    /** Redis 键 */
    public static final class RedisKeys {
        public static final String CACHE_MERCHANT = "cache:merchant:";
        public static final String CACHE_MENU = "cache:menu:";
        public static final String PAY_DEDUP = "pay:dedup:";
        public static final String NOTIFY_DEDUP = "notify:dedup:";
        public static final String STOCK_PRE = "stock:pre:";
        public static final String RATE_LIMIT = "rl:";

        private RedisKeys() {
        }
    }

    /** 通用状态 */
    public static final class Flag {
        public static final int YES = 1;
        public static final int NO = 0;

        private Flag() {
        }
    }
}
