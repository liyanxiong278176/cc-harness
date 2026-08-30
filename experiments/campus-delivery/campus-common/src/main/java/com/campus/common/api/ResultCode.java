package com.campus.common.api;

import lombok.Getter;

/**
 * 规范错误码。
 * 结构: 模块(2位)+场景(2位)+序号(2位);详见 docs/api.md §错误码规范。
 */
@Getter
public enum ResultCode {

    SUCCESS(0, "success"),

    // 通用
    BAD_PARAM(40000, "参数校验失败"),
    PARAM_FORMAT(40001, "参数格式错误"),
    UNAUTHORIZED(40101, "未登录或登录已过期"),
    FORBIDDEN(40301, "无权限执行该操作"),
    NOT_FOUND(40401, "资源不存在"),
    CONFLICT(40901, "数据冲突,请重试"),
    TOO_MANY_REQUESTS(42901, "请求过于频繁"),
    INTERNAL_ERROR(50000, "系统内部错误"),
    DB_ERROR(50001, "数据库异常"),
    ADAPTER_ERROR(50002, "外部模拟服务调用失败"),

    // 用户域 1xxxxx
    USERNAME_EXISTS(100101, "用户名已存在"),
    LOGIN_FAILED(100102, "用户名或密码错误"),
    ACCOUNT_DISABLED(100103, "账号已被禁用"),
    ADDRESS_LIMIT(100104, "地址数量已达上限"),
    ADDRESS_NOT_OWNED(100105, "地址不存在或不属于当前用户"),
    USER_NOT_FOUND(100106, "用户不存在"),
    OLD_PASSWORD_WRONG(100107, "原密码错误"),

    // 商家域 2xxxxx
    MERCHANT_NOT_FOUND(200101, "店铺不存在"),
    MERCHANT_NO_PERMISSION(200102, "无店铺管理权限"),
    MERCHANT_NOT_OPEN_TIME(200103, "当前不在营业时间"),
    MERCHANT_CLOSED(200104, "店铺已打烊"),
    CATEGORY_NOT_FOUND(200105, "分类不存在"),
    CATEGORY_HAS_DISHES(200106, "分类下存在菜品,无法删除"),

    // 菜品域 3xxxxx
    DISH_NOT_FOUND(300101, "菜品不存在"),
    DISH_OFF_SALE(300102, "菜品已下架"),
    STOCK_NOT_ENOUGH(300103, "库存不足"),

    // 购物车/订单域 4xxxxx
    CART_EMPTY(400101, "购物车为空"),
    CART_MERCHANT_MIXED(400102, "购物车包含多个店铺,请分开下单"),
    BELOW_MIN_AMOUNT(400103, "未达起送金额"),
    COUPON_INVALID(400104, "优惠券不可用"),
    ORDER_STATUS_INVALID(400105, "订单状态不允许该操作"),
    ORDER_NOT_FOUND(400106, "订单不存在"),
    ORDER_TIMEOUT(400107, "订单超时未支付,已自动取消"),
    ORDER_ALREADY_PAID(400108, "订单已支付,请勿重复支付"),

    // 支付/退款域 5xxxxx
    PAY_CHANNEL_ERROR(500101, "支付渠道异常"),
    REFUND_EXISTS(500102, "退款申请已存在"),
    REFUND_AMOUNT_INVALID(500103, "退款金额非法"),
    REFUND_NOT_PAID(500104, "订单未支付,无法退款"),

    // 配送域 6xxxxx
    DELIVERY_NO_TASK(600101, "当前无待接单任务"),
    DELIVERY_GRABBED(600102, "任务已被其他骑手接单"),
    DELIVERY_STATUS_INVALID(600103, "配送状态不允许该操作"),

    // 评价域 7xxxxx
    REVIEW_ORDER_NOT_COMPLETED(700101, "订单未完成,不可评价"),
    REVIEW_ALREADY(700102, "该订单已评价"),

    // 通知域 8xxxxx
    NOTIFICATION_NOT_FOUND(800101, "通知不存在");

    private final int code;
    private final String message;

    ResultCode(int code, String message) {
        this.code = code;
        this.message = message;
    }
}
