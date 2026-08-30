package com.campus.web.it;

import com.campus.common.util.PasswordUtils;
import com.campus.dao.entity.Dish;
import com.campus.dao.entity.Merchant;
import com.campus.dao.entity.MerchantEmployee;
import com.campus.dao.entity.SysUser;
import com.campus.dao.mapper.DishMapper;
import com.campus.dao.mapper.MerchantEmployeeMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.SysUserMapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import java.math.BigDecimal;
import java.util.Random;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 集成测试基类: H2(MySQL 模式)内存库 + MockMvc + RabbitTemplate 打桩。
 * 无外部中间件依赖。子类按链路编写用例。
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
public abstract class BaseIT {

    @Autowired
    protected MockMvc mvc;
    @Autowired
    protected ObjectMapper objectMapper;
    @Autowired
    protected SysUserMapper sysUserMapper;
    @Autowired
    protected MerchantMapper merchantMapper;
    @Autowired
    protected MerchantEmployeeMapper employeeMapper;
    @Autowired
    protected DishMapper dishMapper;
    /** Rabbit 打桩: 断言事件发布,不真正连 broker。 */
    @MockBean
    protected RabbitTemplate rabbitTemplate;

    @Autowired
    private org.springframework.jdbc.core.JdbcTemplate jdbcTemplate;

    private static final String[] TABLES = {
            "operation_log", "mq_message", "notification", "review", "delivery_task",
            "refund_record", "payment_record", "order_item", "order_info", "cart",
            "user_coupon", "coupon", "stock_change_log", "dish", "dish_category",
            "merchant_employee", "merchant", "user_address", "sys_user"
    };

    private final Random random = new Random();

    @BeforeEach
    void cleanState() {
        for (String t : TABLES) {
            jdbcTemplate.execute("DELETE FROM `" + t + "`");
        }
    }

    protected String login(String username, String password) throws Exception {
        MvcResult r = mvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk()).andReturn();
        return json(r).path("data.token").asText();
    }

    protected String registerUser() throws Exception {
        String u = "it_user_" + random.nextInt(100_000);
        MvcResult r = mvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + u + "\",\"password\":\"123456\",\"phone\":\"1390000"
                                + (1000 + random.nextInt(9000)) + "\",\"role\":\"USER\"}"))
                .andExpect(status().isOk()).andReturn();
        return json(r).path("data.token").asText();
    }

    /** 直接建商家账号(注册接口固定 USER 角色,商家账号走种子/管理员通道)。 */
    protected Long insertMerchantUser(String merchantAccount) {
        SysUser u = new SysUser();
        u.setUsername(merchantAccount);
        u.setPasswordHash(PasswordUtils.encode("123456"));
        u.setRole("MERCHANT");
        u.setStatus(1);
        sysUserMapper.insert(u);
        Merchant m = new Merchant();
        m.setName("IT店铺-" + merchantAccount);
        m.setCampusZone("东区");
        m.setDeliveryFee(new BigDecimal("2.00"));
        m.setMinOrderAmount(new BigDecimal("0.00"));
        m.setIsOpen(1);
        merchantMapper.insert(m);
        MerchantEmployee e = new MerchantEmployee();
        e.setUserId(u.getId());
        e.setMerchantId(m.getId());
        e.setRole("OWNER");
        employeeMapper.insert(e);
        return m.getId();
    }

    protected Long insertDish(Long merchantId, String name, BigDecimal price, int stock) {
        Dish d = new Dish();
        d.setMerchantId(merchantId);
        d.setCategoryId(0L);
        d.setName(name);
        d.setPrice(price);
        d.setStock(stock);
        d.setSoldCount(0);
        d.setStatus(1);
        dishMapper.insert(d);
        return d.getId();
    }

    protected JsonNode json(MvcResult r) throws Exception {
        return objectMapper.readTree(r.getResponse().getContentAsString());
    }

    /** 创建一条默认用户地址,返回地址 id。 */
    protected Long createAddress(String token, String phoneSuffix) throws Exception {
        JsonNode r = json(mvc.perform(post("/api/user/addresses")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"receiverName\":\"张三\",\"receiverPhone\":\"1390000" + phoneSuffix + "\","
                                + "\"campusZone\":\"东区\",\"detail\":\"1栋101\",\"isDefault\":1}"))
                .andExpect(status().isOk()).andReturn());
        return r.path("data").path("id").asLong();
    }
}
