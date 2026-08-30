package com.campus.web.it;

import com.campus.dao.entity.Coupon;
import com.campus.dao.entity.UserCoupon;
import com.campus.dao.mapper.CouponMapper;
import com.campus.dao.mapper.UserCouponMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 优惠券: 领取 -> 结算核销 -> 已核销券不可复用。 */
class CouponFlowIT extends BaseIT {

    @Autowired
    private CouponMapper couponMapper;
    @Autowired
    private UserCouponMapper userCouponMapper;

    private Long insertCouponTemplate() {
        Coupon c = new Coupon();
        c.setName("IT满10减5");
        c.setType("FULL_REDUCTION");
        c.setThresholdAmount(new BigDecimal("10.00"));
        c.setDiscountAmount(new BigDecimal("5.00"));
        c.setDiscountRate(new BigDecimal("1.000"));
        c.setTotalCount(100);
        c.setIssuedCount(0);
        c.setStartTime(LocalDateTime.now().minusDays(1));
        c.setEndTime(LocalDateTime.now().plusDays(7));
        c.setStatus(1);
        couponMapper.insert(c);
        return c.getId();
    }

    @Test
    void receiveAndUseCouponAtCheckout() throws Exception {
        String userToken = registerUser();
        Long couponTmplId = insertCouponTemplate();
        Long merchantId = insertMerchantUser("it_merchant_c1");
        Long dishId = insertDish(merchantId, "椒麻鸡", new BigDecimal("20.00"), 5);

        // 领取
        mvc.perform(post("/api/user/coupons/" + couponTmplId + "/receive")
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk());

        // 查用户券 id
        List<UserCoupon> ucs = userCouponMapper.selectList(new LambdaQueryWrapper<UserCoupon>()
                .eq(UserCoupon::getUserId, userId(userToken)));
        assertFalse(ucs.isEmpty());
        Long userCouponId = ucs.get(0).getId();

        // 加购
        mvc.perform(post("/api/cart/items")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"dishId\":" + dishId + ",\"quantity\":1}"))
                .andExpect(status().isOk());
        Long addrId = createAddress(userToken, "3333");

        // 结算带券
        JsonNode co = json(mvc.perform(post("/api/orders/checkout")
                        .header("Authorization", "Bearer " + userToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"merchantId\":" + merchantId + ",\"addressId\":" + addrId
                                + ",\"couponId\":" + userCouponId + "}"))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, co.path("code").asInt());
        String orderNo = co.path("data").asText(); // 契约: Result<String>,data 直接为订单号

        // payAmount = 20 + 2 - 5 = 17
        JsonNode detail = json(mvc.perform(get("/api/orders/" + orderNo)
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk()).andReturn());
        assertEquals(0, new BigDecimal("17.00").compareTo(
                new BigDecimal(detail.path("data").path("payAmount").asText())));
        assertEquals(0, new BigDecimal("5.00").compareTo(
                new BigDecimal(detail.path("data").path("discountAmount").asText())));

        // 券已核销,不可再次使用
        UserCoupon used = userCouponMapper.selectById(userCouponId);
        assertEquals("USED", used.getStatus());
    }

    private Long userId(String token) throws Exception {
        JsonNode me = json(mvc.perform(get("/api/auth/me").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk()).andReturn());
        return me.path("data").path("id").asLong();
    }
}
