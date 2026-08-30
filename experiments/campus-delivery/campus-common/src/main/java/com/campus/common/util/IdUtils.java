package com.campus.common.util;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

/**
 * ID 工具: 业务订单号、支付交易号、MQ 消息幂等 ID。
 */
public final class IdUtils {

    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyyMMddHHmmssSSS");

    private IdUtils() {
    }

    /** 业务单号: 时间戳(17位)+ 4位随机 = 21位数字。 */
    public static String orderNo() {
        return TS.format(LocalDateTime.now()) + String.format("%04d", ThreadLocalRandom.current().nextInt(10000));
    }

    /** 渠道交易号(模拟支付): "T" + 21位。 */
    public static String tradeNo() {
        return "T" + orderNo();
    }

    /**
     * MQ 消息幂等 ID(outbox 幂等锚点,对应 mq_message.msg_id 的 uk_msg_id 唯一键):
     * "M" + 32位无横线 UUID,共 33 位,小于 VARCHAR(64) 上限;
     * 唯一性由 UUID 保证,天然避免跨生产者/跨毫秒碰撞(碰撞会导致消息被当作重复丢弃)。
     */
    public static String msgId() {
        return "M" + UUID.randomUUID().toString().replace("-", "");
    }
}
