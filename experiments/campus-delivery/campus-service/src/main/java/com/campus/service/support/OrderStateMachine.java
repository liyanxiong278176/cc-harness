package com.campus.service.support;

import com.campus.common.constant.Constants;

import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/**
 * 订单状态机(纯逻辑,可单测;与 logic-harness/order-state-machine.test.js 语义一致)。
 *
 * 迁移表:
 *   CREATED   -> PAID / CANCELLED / (自动取消)
 *   PAID      -> PREPARING / REFUNDING
 *   PREPARING -> DELIVERING / REFUNDING
 *   DELIVERING-> COMPLETED / REFUNDING
 *   REFUNDING -> REFUNDED
 * 终态: COMPLETED / CANCELLED / REFUNDED
 */
public final class OrderStateMachine {

    private static final Map<String, Set<String>> TRANSITIONS = new HashMap<>();

    static {
        put(Constants.OrderStatus.CREATED,
                Constants.OrderStatus.PAID, Constants.OrderStatus.CANCELLED);
        put(Constants.OrderStatus.PAID,
                Constants.OrderStatus.PREPARING, Constants.OrderStatus.REFUNDING);
        put(Constants.OrderStatus.PREPARING,
                Constants.OrderStatus.DELIVERING, Constants.OrderStatus.REFUNDING);
        put(Constants.OrderStatus.DELIVERING,
                Constants.OrderStatus.COMPLETED, Constants.OrderStatus.REFUNDING);
        put(Constants.OrderStatus.REFUNDING,
                Constants.OrderStatus.REFUNDED, Constants.OrderStatus.PAID);
    }

    private static void put(String from, String... to) {
        TRANSITIONS.put(from, new HashSet<>(Arrays.asList(to)));
    }

    private OrderStateMachine() {
    }

    public static boolean canTransit(String from, String to) {
        Set<String> allowed = TRANSITIONS.get(from);
        return allowed != null && allowed.contains(to);
    }

    public static boolean isFinal(String status) {
        return Constants.OrderStatus.COMPLETED.equals(status)
                || Constants.OrderStatus.CANCELLED.equals(status)
                || Constants.OrderStatus.REFUNDED.equals(status);
    }
}
