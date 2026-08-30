package com.campus.service.adapter;

import java.math.BigDecimal;
import java.util.Map;

/**
 * 支付网关抽象(模拟)。真实实现可实现本接口并通过配置切换。
 */
public interface PaymentGateway {

    /**
     * 创建支付单。
     *
     * @param orderNo 业务订单号
     * @param amount  支付金额
     * @param channel 渠道(如 WECHAT_MOCK / ALIPAY_MOCK)
     * @return 渠道交易号 tradeNo 与支付参数(给前端展示的模拟二维码/URL)
     */
    PaymentResult createPayment(String orderNo, BigDecimal amount, String channel);

    /** 查询支付结果(模拟: 返回 SUCCESS)。 */
    String query(String tradeNo);

    /** 退款(模拟: 返回渠道退款流水号)。 */
    String refund(String tradeNo, BigDecimal amount);

    /** 支持的渠道列表。 */
    String channel();

    final class PaymentResult {
        private final String tradeNo;
        private final Map<String, String> payParams;

        public PaymentResult(String tradeNo, Map<String, String> payParams) {
            this.tradeNo = tradeNo;
            this.payParams = payParams;
        }

        public String getTradeNo() {
            return tradeNo;
        }

        public Map<String, String> getPayParams() {
            return payParams;
        }
    }
}
