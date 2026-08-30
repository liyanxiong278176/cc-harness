package com.campus.service.support;

import com.campus.common.constant.Constants;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 配送状态机契约: 送达必须依次 PICKED->DELIVERING->DELIVERED,禁止跨级。 */
class DeliveryStateMachineTest {

    @Test
    void pickedCanTransitToDelivering() {
        assertTrue(DeliveryStateMachine.canTransit(
                Constants.DeliveryStatus.PICKED, Constants.DeliveryStatus.DELIVERING));
    }

    @Test
    void deliveringCanTransitToDelivered() {
        assertTrue(DeliveryStateMachine.canTransit(
                Constants.DeliveryStatus.DELIVERING, Constants.DeliveryStatus.DELIVERED));
    }

    @Test
    void pickedCannotJumpDirectlyToDelivered() {
        assertFalse(DeliveryStateMachine.canTransit(
                Constants.DeliveryStatus.PICKED, Constants.DeliveryStatus.DELIVERED));
    }
}
