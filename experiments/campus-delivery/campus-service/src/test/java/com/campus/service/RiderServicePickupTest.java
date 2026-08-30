package com.campus.service;

import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.conditions.ISqlSegment;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.dao.entity.DeliveryTask;
import com.campus.dao.mapper.DeliveryTaskMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.service.mq.OrderEventPublisher;
import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.LocalDateTime;
import java.util.Map;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 回归测试: RiderService.pickup 必须原子 CAS ACCEPTED->PICKED(同时持久化 status 与 picked_time)。
 * 修复前: update 只 set picked_time,不带 eq(status,ACCEPTED) 条件也不 set status,
 * DB 状态停留在 ACCEPTED,导致后续 deliver 第一段 CAS(eq PICKED)命中 0 行抛 600103。
 * 注: 类型安全捕获使用 ArgumentCaptor<LambdaUpdateWrapper<DeliveryTask>>,
 * 参数值通过 LambdaUpdateWrapper 继承自 AbstractWrapper 的 getParamNameValuePairs() 读取
 * (不要在 Wrapper 接口类型上调用该方法)。
 */
class RiderServicePickupTest {

    private final DeliveryTaskMapper taskMapper = mock(DeliveryTaskMapper.class);
    private final OrderInfoMapper orderMapper = mock(OrderInfoMapper.class);
    private final SysUserMapper userMapper = mock(SysUserMapper.class);
    private final MerchantMapper merchantMapper = mock(MerchantMapper.class);
    private final OrderEventPublisher publisher = mock(OrderEventPublisher.class);

    private RiderService newService() {
        return new RiderService(taskMapper, orderMapper, userMapper, merchantMapper, publisher);
    }

    /** Mockito 单测无 Spring 容器: 需先初始化 MyBatis-Plus TableInfo,否则 LambdaUpdateWrapper 解析列名抛 MybatisPlusException(can not find lambda cache)。 */
    @BeforeAll
    static void initMybatisPlusTableInfo() {
        MapperBuilderAssistant assistant = new MapperBuilderAssistant(new MybatisConfiguration(), "");
        TableInfoHelper.initTableInfo(assistant, DeliveryTask.class);
    }

    private DeliveryTask task(Long id, Long riderId, String status) {
        DeliveryTask t = new DeliveryTask();
        t.setId(id);
        t.setOrderId(1L);
        t.setOrderNo("IT20260829000001");
        t.setMerchantId(10L);
        t.setUserId(20L);
        t.setRiderId(riderId);
        t.setStatus(status);
        return t;
    }

    @Test
    void pickupAcceptedTaskPersistsPickedStatusViaCas() {
        DeliveryTask accepted = task(7L, 99L, Constants.DeliveryStatus.ACCEPTED);
        when(taskMapper.selectById(7L)).thenReturn(accepted);
        when(taskMapper.update(isNull(), any())).thenReturn(1);

        RiderService svc = newService();
        assertDoesNotThrow(() -> svc.pickup(99L, 7L));

        // 类型安全捕获: 断言 CAS 同时携带 eq(status,ACCEPTED)、set(status,PICKED)、set(pickedTime)
        ArgumentCaptor<LambdaUpdateWrapper<DeliveryTask>> captor =
                ArgumentCaptor.forClass(LambdaUpdateWrapper.class);
        verify(taskMapper, times(1)).update(isNull(), captor.capture());
        LambdaUpdateWrapper<DeliveryTask> wrapper = captor.getValue();
        // SQL 结构: status 条件列必须存在(列名是字面量)
        String normal = wrapper.getExpression().getNormal().stream()
                .map(ISqlSegment::getSqlSegment).collect(Collectors.joining(" "));
        assertTrue(normal.contains("status"), "CAS 必须带 status 条件: " + normal);
        // eq/set 的实际值以 #{ew.paramNameValuePairs.MPGENVALn} 占位符形式出现在 SQL,
        // 因此从参数映射断言值(不断言 SQL 段中的字面量)
        Map<String, Object> params = wrapper.getParamNameValuePairs();
        assertTrue(params.containsValue(Constants.DeliveryStatus.ACCEPTED),
                "eq(status,ACCEPTED) 参数值必须存在: " + params);
        assertTrue(params.containsValue(Constants.DeliveryStatus.PICKED),
                "set(status,PICKED) 参数值必须存在: " + params);
        assertTrue(params.values().stream().anyMatch(v -> v instanceof LocalDateTime),
                "set(pickedTime) 参数值必须存在: " + params);
        verify(publisher, times(1)).publish(any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void pickupThrowsWhenCasFails() {
        DeliveryTask accepted = task(7L, 99L, Constants.DeliveryStatus.ACCEPTED);
        when(taskMapper.selectById(7L)).thenReturn(accepted);
        when(taskMapper.update(isNull(), any())).thenReturn(0); // 并发抢占: ACCEPTED 已不匹配

        RiderService svc = newService();
        assertThrows(BizException.class, () -> svc.pickup(99L, 7L));
        verify(publisher, never()).publish(any(), any(), any(), any(), any(), any(), any());
    }
}
