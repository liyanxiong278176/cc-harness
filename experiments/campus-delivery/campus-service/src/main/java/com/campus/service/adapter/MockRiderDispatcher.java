package com.campus.service.adapter;

import com.campus.dao.entity.SysUser;
import com.campus.dao.mapper.SysUserMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.constant.Constants;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * 模拟骑手调度(app.adapter.dispatch=mock): 找一位在线且任务最少的骑手。
 */
@Component
@ConditionalOnProperty(name = "app.adapter.dispatch", havingValue = "mock", matchIfMissing = true)
public class MockRiderDispatcher implements RiderDispatcher {

    private static final Logger log = LoggerFactory.getLogger(MockRiderDispatcher.class);

    private final SysUserMapper sysUserMapper;

    public MockRiderDispatcher(SysUserMapper sysUserMapper) {
        this.sysUserMapper = sysUserMapper;
    }

    @Override
    public long dispatch(Long orderId, String orderNo, Long merchantId) {
        List<SysUser> riders = sysUserMapper.selectList(new LambdaQueryWrapper<SysUser>()
                .eq(SysUser::getRole, Constants.UserRole.RIDER)
                .eq(SysUser::getStatus, 1));
        if (riders.isEmpty()) {
            log.info("[mock-dispatch] no available rider for order {}", orderNo);
            return 0L;
        }
        // 简化: 轮询取第一个可用骑手
        long riderId = riders.get((int) (orderId % riders.size())).getId();
        log.info("[mock-dispatch] assign rider {} to order {}", riderId, orderNo);
        return riderId;
    }
}
