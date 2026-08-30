package com.campus.web.it;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 支付回调: 幂等;重复回调不重复流转;支付成功后订单进入 PAID。 */
class PaymentCallbackIT extends BaseIT {

    private String createOrderNo(String userToken, String tag) throws Exception {
        Long merchantId = insertMerchantUser("it_merchant_" + tag);
        Long dishId = insertDish(merchantId, "意面" + tag, new BigDecimal("20.00"), 5);
        mvc.perform(post("/api/cart/items")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"dishId\":" + dishId + ",\"quantity\":1}"))
                .andExpect(status().isOk());
        Long addrId = createAddress(userToken, tag);
        JsonNode co = json(mvc.perform(post("/api/orders/checkout")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"merchantId\":" + merchantId + ",\"addressId\":" + addrId + "}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, co.path("code").asInt());
        return co.path("data").asText(); // 契约: Result<String>,data 直接为订单号
    }

    @Test
    void notifyMarksPaidAndIsIdempotent() throws Exception {
        String userToken = registerUser();
        String orderNo = createOrderNo(userToken, "7777");

        String payload = "{\"orderNo\":\"" + orderNo + "\",\"success\":true,\"channel\":\"mock-alipay\"}";
        // 首次回调
        JsonNode r1 = json(mvc.perform(post("/api/payment/mock/notify")
                        .contentType(MediaType.APPLICATION_JSON).content(payload))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, r1.path("code").asInt());
        assertTrue(r1.path("data").path("success").asBoolean());
        assertEquals("PAID", orderStatus(userToken, orderNo));

        // 重复回调 -> 幂等,仍 PAID 且不抛错
        JsonNode r2 = json(mvc.perform(post("/api/payment/mock/notify")
                        .contentType(MediaType.APPLICATION_JSON).content(payload))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, r2.path("code").asInt());
        assertTrue(r2.path("data").path("success").asBoolean());
        assertEquals("PAID", orderStatus(userToken, orderNo));
    }

    @Test
    void failedNotifyDoesNotTransitionOrder() throws Exception {
        String userToken = registerUser();
        String orderNo = createOrderNo(userToken, "8888");
        JsonNode r = json(mvc.perform(post("/api/payment/mock/notify")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"orderNo\":\"" + orderNo + "\",\"success\":false,\"channel\":\"mock-alipay\"}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, r.path("code").asInt());
        assertTrue(!r.path("data").path("success").asBoolean());
        // 失败回调不应把订单置为 PAID(保持 CREATED)
        assertEquals("CREATED", orderStatus(userToken, orderNo));
    }

    private String orderStatus(String token, String orderNo) throws Exception {
        JsonNode d = json(mvc.perform(get("/api/orders/" + orderNo)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk()).andReturn());
        return d.path("data").path("status").asText();
    }
}
