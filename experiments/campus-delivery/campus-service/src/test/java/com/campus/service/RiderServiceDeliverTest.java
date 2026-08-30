package com.campus.service;

import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.dao.entity.DeliveryTask;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.mapper.DeliveryTaskMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.service.mq.OrderEventPublisher;
import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 回归测试: RiderService.deliver 必须依次执行 PICKED->DELIVERING->DELIVERED。
 * 修复前: transit 只校验不更新内存对象,第二段 transit(task, DELIVERED) 仍用
 * 陈旧的 PICKED 状态校验,抛 DELIVERY_STATUS_INVALID;修复后第一段 CAS 更新成功
 * 会重新读取最新状态再走第二段。
 */
class RiderServiceDeliverTest {

    private final DeliveryTaskMapper taskMapper = mock(DeliveryTaskMapper.class);
    private final OrderInfoMapper orderMapper = mock(OrderInfoMapper.class);
    private final SysUserMapper userMapper = mock(SysUserMapper.class);
    private final MerchantMapper merchantMapper = mock(MerchantMapper.class);
    private final OrderEventPublisher publisher = mock(OrderEventPublisher.class);

    private RiderService newService() {
        return new RiderService(taskMapper, orderMapper, userMapper, merchantMapper, publisher);
    }

    /** Mockito 单测无 Spring 容器: 需先初始化 MyBatis-Plus TableInfo(DeliveryTask 与 OrderInfo 均被 LambdaUpdateWrapper 解析),否则抛 MybatisPlusException(can not find lambda cache)。 */
    @BeforeAll
    static void initMybatisPlusTableInfo() {
        MapperBuilderAssistant assistant = new MapperBuilderAssistant(new MybatisConfiguration(), "");
        TableInfoHelper.initTableInfo(assistant, DeliveryTask.class);
        TableInfoHelper.initTableInfo(assistant, OrderInfo.class);
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
    void deliverPickedTaskCompletesBothTransitions() {
        DeliveryTask picked = task(7L, 99L, Constants.DeliveryStatus.PICKED);
        DeliveryTask delivering = task(7L, 99L, Constants.DeliveryStatus.DELIVERING);
        // requireOwnedTask 首次读到 PICKED;第一段 CAS 更新后重新读取到 DELIVERING(模拟真实 DB)
        when(taskMapper.selectById(7L)).thenReturn(picked, delivering);
        when(taskMapper.update(isNull(), any())).thenReturn(1);
        when(orderMapper.update(isNull(), any())).thenReturn(1);

        RiderService svc = newService();
        assertDoesNotThrow(() -> svc.deliver(99L, 7L));

        // 两段任务状态 CAS 均执行: PICKED->DELIVERING, DELIVERING->DELIVERED
        InOrder inOrder = inOrder(taskMapper);
        inOrder.verify(taskMapper).update(isNull(), any(LambdaUpdateWrapper.class));
        inOrder.verify(taskMapper).update(isNull(), any(LambdaUpdateWrapper.class));
        // 订单 DELIVERING -> COMPLETED
        verify(orderMapper, times(1)).update(isNull(), any(LambdaUpdateWrapper.class));
        // 送达通知发布
        verify(publisher, times(1)).publish(any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void deliverThrowsWhenFirstLegCasFails() {
        DeliveryTask picked = task(7L, 99L, Constants.DeliveryStatus.PICKED);
        when(taskMapper.selectById(7L)).thenReturn(picked);
        when(taskMapper.update(isNull(), any())).thenReturn(0); // 并发抢占: PICKED 已不匹配

        RiderService svc = newService();
        assertThrows(BizException.class, () -> svc.deliver(99L, 7L));

        // 第一段失败即中止,第二段与订单更新不应执行
        verify(taskMapper, times(1)).update(isNull(), any(LambdaUpdateWrapper.class));
        verify(orderMapper, never()).update(any(), any());
        verify(publisher, never()).publish(any(), any(), any(), any(), any(), any(), any());
    }

    /** 全链路回归: pickup(ACCEPTED->PICKED 持久化) 后 deliver(PICKED->DELIVERING->DELIVERED) 必须完整走通。 */
    @Test
    void pickupThenDeliverCompletesFullDeliveryChain() {
        DeliveryTask accepted = task(7L, 99L, Constants.DeliveryStatus.ACCEPTED);
        DeliveryTask picked = task(7L, 99L, Constants.DeliveryStatus.PICKED);
        DeliveryTask delivering = task(7L, 99L, Constants.DeliveryStatus.DELIVERING);
        // pickup: selectById->ACCEPTED; deliver: selectById->PICKED, 第一段后重读->DELIVERING(模拟真实 DB)
        when(taskMapper.selectById(7L)).thenReturn(accepted, picked, delivering);
        when(taskMapper.update(isNull(), any())).thenReturn(1);
        when(orderMapper.update(isNull(), any())).thenReturn(1);

        RiderService svc = newService();
        assertDoesNotThrow(() -> {
            svc.pickup(99L, 7L);
            svc.deliver(99L, 7L);
        });

        // pickup 1 次 CAS + deliver 2 次 CAS = 3 次任务更新;订单 1 次更新;通知 2 次(TASK_PICKED + TASK_DELIVERED)
        verify(taskMapper, times(3)).update(isNull(), any(LambdaUpdateWrapper.class));
        verify(orderMapper, times(1)).update(isNull(), any(LambdaUpdateWrapper.class));
        verify(publisher, times(2)).publish(any(), any(), any(), any(), any(), any(), any());
    }
}
