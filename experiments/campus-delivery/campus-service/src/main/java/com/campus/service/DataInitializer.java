package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.campus.common.util.AesUtils;
import com.campus.common.util.PasswordUtils;
import com.campus.dao.entity.SysUser;
import com.campus.dao.entity.UserAddress;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.dao.mapper.UserAddressMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;

/**
 * 种子数据修复(幂等):
 * 1) 把 db/init/02-seed.sql 中的占位 BCrypt 密码(SEED_PLACEHOLDER)重置为演示密码 123456;
 * 2) 把占位手机号 'E_' 重加密为 AES 密文(明文按账号映射,未命中用默认号)。
 * 受 app.seed.demo-password-reset 开关控制(默认 true)。
 */
@Component
@ConditionalOnProperty(name = "app.seed.demo-password-reset", havingValue = "true", matchIfMissing = true)
public class DataInitializer implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(DataInitializer.class);

    /** 02-seed.sql 中的占位密码常量。 */
    static final String SEED_PLACEHOLDER_HASH = "$2a$10$SEED_PLACEHOLDER_PLEASE_OVERRIDE";
    static final String PHONE_PLACEHOLDER = "E_";
    static final String DEFAULT_DEMO_PHONE = "13800009999";

    private static final Map<String, String> DEMO_PHONES = Map.of(
            "admin", "13800000000",
            "zhangsan", "13800000001",
            "lisi", "13800000002",
            "m_hanbao", "13800000003",
            "m_chuan", "13800000004",
            "rider1", "13800000005",
            "rider2", "13800000006");

    private final SysUserMapper sysUserMapper;
    private final UserAddressMapper userAddressMapper;
    @Value("${app.seed.demo-password-reset:true}")
    private boolean enabled;

    public DataInitializer(SysUserMapper sysUserMapper, UserAddressMapper userAddressMapper) {
        this.sysUserMapper = sysUserMapper;
        this.userAddressMapper = userAddressMapper;
    }

    @Override
    @Transactional
    public void run(ApplicationArguments args) {
        if (!enabled) {
            log.info("[data-init] app.seed.demo-password-reset=false, skip");
            return;
        }
        String demoHash = PasswordUtils.encode("123456");
        // 1) 重置占位密码(幂等: 重置后不再命中)
        int pwdRows = sysUserMapper.update(null, new LambdaUpdateWrapper<SysUser>()
                .eq(SysUser::getPasswordHash, SEED_PLACEHOLDER_HASH)
                .set(SysUser::getPasswordHash, demoHash));
        // 2) 修复占位手机号(幂等: 重加密后不再为 E_)
        List<SysUser> users = sysUserMapper.selectList(new LambdaUpdateWrapper<SysUser>()
                .eq(SysUser::getPhone, PHONE_PLACEHOLDER));
        int phoneRows = 0;
        for (SysUser u : users) {
            String plain = DEMO_PHONES.getOrDefault(u.getUsername(), DEFAULT_DEMO_PHONE);
            sysUserMapper.update(null, new LambdaUpdateWrapper<SysUser>()
                    .eq(SysUser::getId, u.getId())
                    .set(SysUser::getPhone, AesUtils.encrypt(plain)));
            phoneRows++;
        }
        // 3) 修复地址占位手机号
        List<UserAddress> addrs = userAddressMapper.selectList(new LambdaUpdateWrapper<UserAddress>()
                .eq(UserAddress::getReceiverPhone, PHONE_PLACEHOLDER));
        int addrRows = 0;
        for (UserAddress a : addrs) {
            SysUser owner = sysUserMapper.selectById(a.getUserId());
            String plain = owner == null ? DEFAULT_DEMO_PHONE
                    : DEMO_PHONES.getOrDefault(owner.getUsername(), DEFAULT_DEMO_PHONE);
            userAddressMapper.update(null, new LambdaUpdateWrapper<UserAddress>()
                    .eq(UserAddress::getId, a.getId())
                    .set(UserAddress::getReceiverPhone, AesUtils.encrypt(plain)));
            addrRows++;
        }
        if (pwdRows > 0 || phoneRows > 0 || addrRows > 0) {
            log.info("[data-init] seed repair done: password={}, user_phone={}, address_phone={}",
                    pwdRows, phoneRows, addrRows);
        } else {
            log.info("[data-init] seed already initialized, no-op");
        }
    }
}
