package com.campus.service.adapter;

/**
 * 骑手调度抽象(模拟)。
 */
public interface RiderDispatcher {

    /**
     * 为订单分配骑手。
     *
     * @param orderId    订单ID
     * @param orderNo    订单号
     * @param merchantId 商家ID
     * @return 被分配骑手的 user_id;0 表示暂无可分配骑手(进入待接单池)
     */
    long dispatch(Long orderId, String orderNo, Long merchantId);
}
