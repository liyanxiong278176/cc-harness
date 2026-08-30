package com.campus.web.it;

import com.campus.dao.entity.OrderInfo;
import com.campus.dao.mapper.OrderInfoMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.constant.Constants;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 评价流: 订单完成(COMPLETED)后可评价。由于 MQ 消费在测试环境关闭,
 * 派送链路无法自动把订单推到 COMPLETED,此处直接置状态以隔离评价写路径。
 */
class ReviewFlowIT extends BaseIT {

    @Autowired
    private OrderInfoMapper orderInfoMapper;

    @Test
    void reviewCompletedOrderAndRejectDuplicate() throws Exception {
        String userToken = registerUser();
        Long merchantId = insertMerchantUser("it_merchant_r1");
        Long dishId = insertDish(merchantId, "照烧鸡排", new BigDecimal("18.00"), 5);

        mvc.perform(post("/api/cart/items")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"dishId\":" + dishId + ",\"quantity\":1}"))
                .andExpect(status().isOk());
        Long addrId = createAddress(userToken, "5555");
        JsonNode co = json(mvc.perform(post("/api/orders/checkout")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"merchantId\":" + merchantId + ",\"addressId\":" + addrId + "}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, co.path("code").asInt());
        String orderNo = co.path("data").asText(); // 契约: Result<String>,data 直接为订单号

        // 直接置为 COMPLETED(绕过关闭的 MQ 派送链路)
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        order.setStatus(Constants.OrderStatus.COMPLETED);
        orderInfoMapper.updateById(order);

        // 评价
        JsonNode r1 = json(mvc.perform(post("/api/orders/" + orderNo + "/review")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"rating\":5,\"content\":\"很好吃\",\"images\":\"\"}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, r1.path("code").asInt());

        // 重复评价 -> 拒绝
        JsonNode r2 = json(mvc.perform(post("/api/orders/" + orderNo + "/review")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"rating\":4,\"content\":\"重复\"}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(700102, r2.path("code").asInt()); // 重复评价
    }
}
