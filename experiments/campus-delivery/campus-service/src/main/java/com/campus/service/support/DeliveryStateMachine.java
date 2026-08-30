package com.campus.service.support;

import com.campus.common.constant.Constants;

import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/**
 * 配送状态机(纯逻辑)。
 *   WAIT_ACCEPT -> ACCEPTED -> PICKED -> DELIVERING -> DELIVERED
 *   WAIT_ACCEPT -> CANCELLED
 */
public final class DeliveryStateMachine {

    private static final Map<String, Set<String>> TRANSITIONS = new HashMap<>();

    static {
        put(Constants.DeliveryStatus.WAIT_ACCEPT,
                Constants.DeliveryStatus.ACCEPTED, Constants.DeliveryStatus.CANCELLED);
        put(Constants.DeliveryStatus.ACCEPTED, Constants.DeliveryStatus.PICKED);
        put(Constants.DeliveryStatus.PICKED, Constants.DeliveryStatus.DELIVERING);
        put(Constants.DeliveryStatus.DELIVERING, Constants.DeliveryStatus.DELIVERED);
    }

    private static void put(String from, String... to) {
        TRANSITIONS.put(from, new HashSet<>(Arrays.asList(to)));
    }

    private DeliveryStateMachine() {
    }

    public static boolean canTransit(String from, String to) {
        Set<String> allowed = TRANSITIONS.get(from);
        return allowed != null && allowed.contains(to);
    }
}
