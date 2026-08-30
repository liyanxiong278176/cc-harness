package com.campus.web.it;

import com.campus.service.NotificationService;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 站内通知: 未读计数 / 列表 / 已读。 */
class NotificationIT extends BaseIT {

    @Autowired
    private NotificationService notificationService;

    @Test
    void notificationListUnreadCountAndMarkRead() throws Exception {
        String userToken = registerUser();
        Long userId = userId(userToken);

        // 直接创建两条通知(生产链路经 MQ 消费写库,测试环境 Rabbit 打桩故直连服务)
        notificationService.create(userId, "ORDER_STATUS", "订单已创建", "您的订单已创建", "ORDER", "ITORDER1");
        notificationService.create(userId, "ORDER_STATUS", "订单已支付", "支付成功", "ORDER", "ITORDER2");

        // 未读计数 = 2
        JsonNode unread = json(mvc.perform(get("/api/user/notifications/unread-count")
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk()).andReturn());
        assertEquals(2, unread.path("data").asInt());

        // 列表
        JsonNode list = json(mvc.perform(get("/api/user/notifications?page=1&size=10")
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk()).andReturn());
        assertEquals(2, list.path("data").path("records").size());
        Long notifId = list.path("data").path("records").get(0).path("id").asLong();

        // 标记一条已读
        mvc.perform(put("/api/user/notifications/" + notifId + "/read")
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk());

        // 未读计数 = 1
        JsonNode unread2 = json(mvc.perform(get("/api/user/notifications/unread-count")
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk()).andReturn());
        assertEquals(1, unread2.path("data").asInt());
    }

    private Long userId(String token) throws Exception {
        JsonNode me = json(mvc.perform(get("/api/auth/me").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk()).andReturn());
        return me.path("data").path("id").asLong();
    }
}
