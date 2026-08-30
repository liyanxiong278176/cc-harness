package com.campus.web.it;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MvcResult;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 认证流: 注册/登录/me/改密/角色越权。 */
class AuthFlowIT extends BaseIT {

    @Test
    void registerLoginMeChangePassword() throws Exception {
        // 注册
        MvcResult reg = mvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"it_auth\",\"password\":\"123456\",\"phone\":\"13900001234\",\"role\":\"USER\"}"))
                .andExpect(status().isOk()).andReturn();
        JsonNode regJson = json(reg);
        assertEquals(0, regJson.path("code").asInt());
        String token = regJson.path("data").path("token").asText();
        assertFalse(token.isEmpty());

        // me
        JsonNode me = json(mvc.perform(get("/api/auth/me").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk()).andReturn());
        assertEquals("it_auth", me.path("data").path("username").asText());

        // 改密(PUT)
        mvc.perform(put("/api/auth/password")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"oldPassword\":\"123456\",\"newPassword\":\"654321\"}"))
                .andExpect(status().isOk());

        // 旧密码失效,新密码可登录
        mvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"it_auth\",\"password\":\"123456\"}"))
                .andExpect(status().isOk())
                .andExpect(r -> assertEquals(40101, json(r).path("code").asInt()));
        String token2 = login("it_auth", "654321");
        assertNotNull(token2);
    }

    @Test
    void wrongPasswordRejected() throws Exception {
        registerUser();
        MvcResult r = mvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"it_wrong\",\"password\":\"bad\"}"))
                .andExpect(status().isOk()).andReturn();
        assertEquals(40101, json(r).path("code").asInt());
    }

    @Test
    void userRoleCannotAccessMerchantApi() throws Exception {
        String userToken = registerUser();
        MvcResult r = mvc.perform(get("/api/merchant/dashboard")
                        .header("Authorization", "Bearer " + userToken))
                .andExpect(status().isOk()).andReturn();
        assertEquals(40301, json(r).path("code").asInt()); // 角色越权
    }

    @Test
    void noTokenRejected() throws Exception {
        MvcResult r = mvc.perform(get("/api/auth/me")).andExpect(status().isOk()).andReturn();
        assertEquals(40101, json(r).path("code").asInt());
    }
}
