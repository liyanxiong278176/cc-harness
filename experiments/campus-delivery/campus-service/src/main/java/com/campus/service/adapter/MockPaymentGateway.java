package com.campus.service.adapter;

import com.campus.common.util.IdUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.util.HashMap;
import java.util.Map;

/**
 * 模拟支付网关(app.adapter.payment=mock): 创建即成功,提供模拟支付参数;
 * 由 /payment/mock/notify 回调触发成功入账(幂等由 PaymentService 保证)。
 */
@Component
@ConditionalOnProperty(name = "app.adapter.payment", havingValue = "mock", matchIfMissing = true)
public class MockPaymentGateway implements PaymentGateway {

    private static final Logger log = LoggerFactory.getLogger(MockPaymentGateway.class);

    @Override
    public PaymentResult createPayment(String orderNo, BigDecimal amount, String channel) {
        String tradeNo = IdUtils.tradeNo();
        Map<String, String> params = new HashMap<>();
        params.put("mockQr", "https://mock.pay.local/qr/" + tradeNo);
        params.put("mockUrl", "http://localhost:8080/api/payment/mock/notify?orderNo=" + orderNo);
        params.put("channel", channel);
        log.info("[mock-pay] createPayment orderNo={} tradeNo={} amount={}", orderNo, tradeNo, amount);
        return new PaymentResult(tradeNo, params);
    }

    @Override
    public String query(String tradeNo) {
        return "SUCCESS";
    }

    @Override
    public String refund(String tradeNo, BigDecimal amount) {
        return "R" + tradeNo;
    }

    @Override
    public String channel() {
        return "MOCK";
    }
}
