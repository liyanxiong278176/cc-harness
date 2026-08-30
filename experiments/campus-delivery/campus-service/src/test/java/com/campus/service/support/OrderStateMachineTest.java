package com.campus.service.support;

import com.campus.common.constant.Constants;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OrderStateMachineTest {

    @Test
    void happyPathTransitions() {
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.CREATED, Constants.OrderStatus.PAID));
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.PAID, Constants.OrderStatus.PREPARING));
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.PREPARING, Constants.OrderStatus.DELIVERING));
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.DELIVERING, Constants.OrderStatus.COMPLETED));
    }

    @Test
    void cancelOnlyFromCreated() {
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.CREATED, Constants.OrderStatus.CANCELLED));
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.PAID, Constants.OrderStatus.CANCELLED));
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.PREPARING, Constants.OrderStatus.CANCELLED));
    }

    @Test
    void refundOnlyFromInTransitStates() {
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.PAID, Constants.OrderStatus.REFUNDING));
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.PREPARING, Constants.OrderStatus.REFUNDING));
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.DELIVERING, Constants.OrderStatus.REFUNDING));
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.COMPLETED, Constants.OrderStatus.REFUNDING));
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.CREATED, Constants.OrderStatus.REFUNDING));
    }

    @Test
    void refundResolvedToRefundedOrBackToPaid() {
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.REFUNDING, Constants.OrderStatus.REFUNDED));
        assertTrue(OrderStateMachine.canTransit(Constants.OrderStatus.REFUNDING, Constants.OrderStatus.PAID));
    }

    @Test
    void terminalStatesAreFinal() {
        assertTrue(OrderStateMachine.isFinal(Constants.OrderStatus.COMPLETED));
        assertTrue(OrderStateMachine.isFinal(Constants.OrderStatus.CANCELLED));
        assertTrue(OrderStateMachine.isFinal(Constants.OrderStatus.REFUNDED));
        assertFalse(OrderStateMachine.isFinal(Constants.OrderStatus.PAID));
    }

    @Test
    void illegalJumpsRejected() {
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.CREATED, Constants.OrderStatus.COMPLETED));
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.PAID, Constants.OrderStatus.DELIVERING));
        assertFalse(OrderStateMachine.canTransit(Constants.OrderStatus.PREPARING, Constants.OrderStatus.PAID));
    }
}
