package com.campus.web.it;

import com.campus.dao.entity.Dish;
import com.campus.dao.mapper.DishMapper;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;

import java.math.BigDecimal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 下单链路: 加购 -> 结算 -> 库存扣减;重复结算防重。 */
class OrderCheckoutIT extends BaseIT {

    @Autowired
    private DishMapper dishMapper;

    @Test
    void checkoutDeductsStockAndCreatesOrder() throws Exception {
        String userToken = registerUser();
        Long merchantId = insertMerchantUser("it_merchant_1");
        Long dishId = insertDish(merchantId, "香辣鸡腿堡", new BigDecimal("15.00"), 10);

        // 加购
        mvc.perform(post("/api/cart/items")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"dishId\":" + dishId + ",\"quantity\":2}"))
                .andExpect(status().isOk());

        // 建地址
        Long addrId = createAddress(userToken, "1111");

        // 结算
        JsonNode checkout = json(mvc.perform(post("/api/orders/checkout")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"merchantId\":" + merchantId + ",\"addressId\":" + addrId + "}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, checkout.path("code").asInt());
        String orderNo = checkout.path("data").asText(); // 契约: Result<String>,data 直接为订单号

        // 库存扣减 10 -> 8
        Dish after = dishMapper.selectById(dishId);
        assertEquals(8, after.getStock());
        assertEquals(2, after.getSoldCount());

        // 订单明细金额: 15 * 2 = 30,配送费 2 -> payAmount 32
        JsonNode detail = json(mvc.perform(get("/api/orders/" + orderNo)
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk()).andReturn());
        assertEquals("CREATED", detail.path("data").path("status").asText());
        assertEquals(0, new BigDecimal("32.00").compareTo(
                new BigDecimal(detail.path("data").path("payAmount").asText())));
    }

    @Test
    void secondCheckoutOfEmptyCartRejected() throws Exception {
        String userToken = registerUser();
        Long merchantId = insertMerchantUser("it_merchant_2");
        Long addrId = createAddress(userToken, "2222");
        JsonNode r = json(mvc.perform(post("/api/orders/checkout")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"merchantId\":" + merchantId + ",\"addressId\":" + addrId + "}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(400101, r.path("code").asInt()); // 购物车为空
    }
}
