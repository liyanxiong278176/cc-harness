package com.campus.web.config;

import com.campus.common.constant.Constants;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.TopicExchange;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.Map;

/**
 * RabbitMQ 声明式配置(bean 名 = {@code rabbitConfig})。
 *
 * <p>交换机/队列/死信/路由键全部取自 {@link Constants.Mq},由 Spring AMQP 的
 * {@code RabbitAdmin} 在应用启动时自动声明到 Broker。</p>
 *
 * <p><b>注意:</b>campus-service 的 {@code NotificationConsumer} 通过
 * SpEL {@code #{rabbitConfig.queueNotifyOrder.name}} 引用队列名,因此本类必须
 * 暴露名为 {@code queueNotifyOrder/queueNotifyPayment/queueNotifyDelivery/
 * queueNotifySystem} 的 {@link Queue} 字段(SpEL 属性访问),同时以同名
 * {@code @Bean} 方法向容器注册 4 个 Queue bean 供 RabbitAdmin 声明。</p>
 */
@Configuration
public class RabbitConfig {

    /** 死信路由键(主队列死信消息投递到 DLX 时使用)。 */
    private static final String DLX_RK_ORDER = "order.dlq";
    private static final String DLX_RK_NOTIFY = "notify.dlq";

    /**
     * 四个通知队列字段(供 SpEL {@code rabbitConfig.queueNotifyOrder.name} 访问)。
     * 与下方同名 @Bean 方法返回相同的队列名,声明参数一致(持久化)。
     */
    public final Queue queueNotifyOrder = durableQueue(Constants.Mq.QUEUE_NOTIFY_ORDER, DLX_RK_NOTIFY);
    public final Queue queueNotifyPayment = durableQueue(Constants.Mq.QUEUE_NOTIFY_PAYMENT, DLX_RK_NOTIFY);
    public final Queue queueNotifyDelivery = durableQueue(Constants.Mq.QUEUE_NOTIFY_DELIVERY, DLX_RK_NOTIFY);
    public final Queue queueNotifySystem = durableQueue(Constants.Mq.QUEUE_NOTIFY_SYSTEM, DLX_RK_NOTIFY);

    // ---------- 通知队列 beans(SpEL 字段的同名 bean,供 RabbitAdmin 声明) ----------

    @Bean
    public Queue queueNotifyOrder() {
        return durableQueue(Constants.Mq.QUEUE_NOTIFY_ORDER, DLX_RK_NOTIFY);
    }

    @Bean
    public Queue queueNotifyPayment() {
        return durableQueue(Constants.Mq.QUEUE_NOTIFY_PAYMENT, DLX_RK_NOTIFY);
    }

    @Bean
    public Queue queueNotifyDelivery() {
        return durableQueue(Constants.Mq.QUEUE_NOTIFY_DELIVERY, DLX_RK_NOTIFY);
    }

    @Bean
    public Queue queueNotifySystem() {
        return durableQueue(Constants.Mq.QUEUE_NOTIFY_SYSTEM, DLX_RK_NOTIFY);
    }

    // ---------- 订单事件队列 & 死信队列 ----------

    @Bean
    public Queue queueOrderEvents() {
        return durableQueue(Constants.Mq.QUEUE_ORDER_EVENTS, DLX_RK_ORDER);
    }

    @Bean
    public Queue queueOrderDlq() {
        return new Queue(Constants.Mq.QUEUE_ORDER_DLQ, true);
    }

    @Bean
    public Queue queueNotifyDlq() {
        return new Queue(Constants.Mq.QUEUE_NOTIFY_DLQ, true);
    }

    // ---------- 交换机 ----------

    @Bean
    public DirectExchange orderExchange() {
        return new DirectExchange(Constants.Mq.EXCHANGE_ORDER, true, false);
    }

    @Bean
    public TopicExchange notifyExchange() {
        return new TopicExchange(Constants.Mq.EXCHANGE_NOTIFY, true, false);
    }

    @Bean
    public DirectExchange dlxExchange() {
        return new DirectExchange(Constants.Mq.EXCHANGE_DLX, true, false);
    }

    // ---------- 订单事件绑定(EXCHANGE_ORDER) ----------

    @Bean
    public Binding orderCreatedBinding() {
        return BindingBuilder.bind(queueOrderEvents()).to(orderExchange())
                .with(Constants.Mq.RK_ORDER_CREATED);
    }

    @Bean
    public Binding orderPaidBinding() {
        return BindingBuilder.bind(queueOrderEvents()).to(orderExchange())
                .with(Constants.Mq.RK_ORDER_PAID);
    }

    @Bean
    public Binding orderStatusBinding() {
        return BindingBuilder.bind(queueOrderEvents()).to(orderExchange())
                .with(Constants.Mq.RK_ORDER_STATUS);
    }

    // ---------- 通知队列绑定(EXCHANGE_NOTIFY) ----------

    @Bean
    public Binding notifyOrderCreatedBinding() {
        return BindingBuilder.bind(queueNotifyOrder()).to(notifyExchange())
                .with(Constants.Mq.RK_ORDER_CREATED);
    }

    @Bean
    public Binding notifyOrderStatusBinding() {
        return BindingBuilder.bind(queueNotifyOrder()).to(notifyExchange())
                .with(Constants.Mq.RK_ORDER_STATUS);
    }

    @Bean
    public Binding notifyPaymentBinding() {
        return BindingBuilder.bind(queueNotifyPayment()).to(notifyExchange())
                .with(Constants.Mq.RK_ORDER_PAID);
    }

    @Bean
    public Binding notifyRefundBinding() {
        return BindingBuilder.bind(queueNotifyPayment()).to(notifyExchange())
                .with(Constants.Mq.RK_REFUND);
    }

    @Bean
    public Binding notifyDeliveryBinding() {
        return BindingBuilder.bind(queueNotifyDelivery()).to(notifyExchange())
                .with(Constants.Mq.RK_DELIVERY);
    }

    @Bean
    public Binding notifySystemBinding() {
        // SYSTEM 类通知暂无业务发送方,预留通配路由
        return BindingBuilder.bind(queueNotifySystem()).to(notifyExchange())
                .with("system.*");
    }

    // ---------- 死信队列绑定(EXCHANGE_DLX) ----------

    @Bean
    public Binding orderDlqBinding() {
        return BindingBuilder.bind(queueOrderDlq()).to(dlxExchange()).with(DLX_RK_ORDER);
    }

    @Bean
    public Binding notifyDlqBinding() {
        return BindingBuilder.bind(queueNotifyDlq()).to(dlxExchange()).with(DLX_RK_NOTIFY);
    }

    // ---------- 工具方法 ----------

    /** 持久化队列,并挂死信交换机与死信路由键。 */
    private static Queue durableQueue(String name, String dlxRoutingKey) {
        return new Queue(name, true, false, false,
                Map.of("x-dead-letter-exchange", Constants.Mq.EXCHANGE_DLX,
                        "x-dead-letter-routing-key", dlxRoutingKey));
    }
}
