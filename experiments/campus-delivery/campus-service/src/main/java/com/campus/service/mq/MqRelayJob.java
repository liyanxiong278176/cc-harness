package com.campus.service.mq;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.constant.Constants;
import com.campus.dao.entity.MqMessage;
import com.campus.dao.mapper.MqMessageMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 本地消息投递任务(Relay): 轮询 PENDING 消息投递到 RabbitMQ。
 * - 成功 -> SENT;
 * - 失败 -> 重试(指数退避),超过最大次数 -> 标记 FAILED(进入死信语义)。
 * app.mq.enabled=false 时关闭(测试/无 MQ 环境)。
 */
@Component
@ConditionalOnProperty(name = "app.mq.enabled", havingValue = "true", matchIfMissing = true)
public class MqRelayJob {

    private static final Logger log = LoggerFactory.getLogger(MqRelayJob.class);
    private static final int MAX_RETRY = 5;
    private static final int BATCH = 50;

    private final MqMessageMapper mqMessageMapper;
    private final RabbitTemplate rabbitTemplate;

    public MqRelayJob(MqMessageMapper mqMessageMapper, RabbitTemplate rabbitTemplate) {
        this.mqMessageMapper = mqMessageMapper;
        this.rabbitTemplate = rabbitTemplate;
    }

    @Scheduled(fixedDelay = 3000, initialDelay = 5000)
    public void relay() {
        List<MqMessage> pending = mqMessageMapper.selectList(new LambdaQueryWrapper<MqMessage>()
                .eq(MqMessage::getStatus, Constants.MqMsgStatus.PENDING)
                .le(MqMessage::getNextRetryTime, LocalDateTime.now())
                .last("LIMIT " + BATCH));
        for (MqMessage msg : pending) {
            try {
                rabbitTemplate.convertAndSend(msg.getExchange(), msg.getRoutingKey(), msg.getPayload());
                mark(msg, Constants.MqMsgStatus.SENT, null, null);
            } catch (Exception e) {
                int retry = msg.getRetryCount() + 1;
                boolean dead = retry >= MAX_RETRY;
                mark(msg, dead ? Constants.MqMsgStatus.FAILED : Constants.MqMsgStatus.PENDING,
                        retry, truncate(e.getMessage()));
                if (dead) {
                    log.error("[mq-relay] message {} moved to FAILED(DLQ 语义) rk={} err={}",
                            msg.getMsgId(), msg.getRoutingKey(), e.getMessage());
                } else {
                    log.warn("[mq-relay] message {} retry {}/{} rk={} err={}",
                            msg.getMsgId(), retry, MAX_RETRY, msg.getRoutingKey(), e.getMessage());
                }
            }
        }
    }

    private void mark(MqMessage msg, String status, Integer retryCount, String lastError) {
        MqMessage upd = new MqMessage();
        upd.setId(msg.getId());
        upd.setStatus(status);
        if (retryCount != null) {
            upd.setRetryCount(retryCount);
            // 指数退避: 2^retry * 3s
            upd.setNextRetryTime(LocalDateTime.now().plusSeconds((long) Math.pow(2, retryCount) * 3));
        }
        if (lastError != null) {
            upd.setLastError(lastError);
        }
        mqMessageMapper.updateById(upd);
    }

    private static String truncate(String s) {
        if (s == null) {
            return "";
        }
        return s.length() > 480 ? s.substring(0, 480) : s;
    }
}
