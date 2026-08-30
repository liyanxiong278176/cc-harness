package com.campus.service.adapter;

/**
 * 短信网关抽象(模拟)。
 */
public interface SmsSender {

    /**
     * 发送短信。
     *
     * @param phone 接收号码(加密前或明文,由实现决定)
     * @param scene 场景码(LOGIN/ORDER/PAYMENT/DELIVERY)
     * @param params 模板参数
     */
    void send(String phone, String scene, java.util.Map<String, String> params);
}
