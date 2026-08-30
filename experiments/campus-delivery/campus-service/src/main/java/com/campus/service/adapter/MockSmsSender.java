package com.campus.service.adapter;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * 模拟短信网关: 仅打印日志,不触达真实运营商。
 */
@Component
@ConditionalOnProperty(name = "app.adapter.sms", havingValue = "mock", matchIfMissing = true)
public class MockSmsSender implements SmsSender {

    private static final Logger log = LoggerFactory.getLogger(MockSmsSender.class);

    @Override
    public void send(String phone, String scene, java.util.Map<String, String> params) {
        // 日志不打印明文手机号
        log.info("[mock-sms] scene={} params={}", scene, params == null ? "{}" : params.keySet());
    }
}
