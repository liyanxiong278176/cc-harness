package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.model.PageQuery;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.dao.entity.DeliveryTask;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.entity.SysUser;
import com.campus.dao.mapper.DeliveryTaskMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.service.mq.OrderEventPublisher;
import com.campus.service.support.DeliveryStateMachine;
import com.campus.service.vo.TaskVO;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * 配送服务(模拟配送域): 派单、待接单池、抢单(条件更新防双抢)、取餐、送达。
 * 状态机: WAIT_ACCEPT -> ACCEPTED -> PICKED -> DELIVERING -> DELIVERED(DeliveryStateMachine)。
 * 送达时联动订单 DELIVERING -> COMPLETED(OrderStateMachine)。
 */
@Service
public class RiderService {

    private static final Logger log = LoggerFactory.getLogger(RiderService.class);

    private final DeliveryTaskMapper deliveryTaskMapper;
    private final OrderInfoMapper orderInfoMapper;
    private final SysUserMapper sysUserMapper;
    private final MerchantMapper merchantMapper;
    private final OrderEventPublisher orderEventPublisher;

    public RiderService(DeliveryTaskMapper deliveryTaskMapper,
                        OrderInfoMapper orderInfoMapper,
                        SysUserMapper sysUserMapper,
                        MerchantMapper merchantMapper,
                        OrderEventPublisher orderEventPublisher) {
        this.deliveryTaskMapper = deliveryTaskMapper;
        this.orderInfoMapper = orderInfoMapper;
        this.sysUserMapper = sysUserMapper;
        this.merchantMapper = merchantMapper;
        this.orderEventPublisher = orderEventPublisher;
    }

    /**
     * 出餐派单: 创建 WAIT_ACCEPT 任务;若有在线骑手则按「最小在途负载」预分配,
     * 否则进入开放抢单池(rider_id=0)。
     */
    @Transactional
    public void dispatch(OrderInfo order, String pickupAddress, String deliveryAddress) {
        DeliveryTask task = new DeliveryTask();
        task.setOrderId(order.getId());
        task.setOrderNo(order.getOrderNo());
        task.setMerchantId(order.getMerchantId());
        task.setUserId(order.getUserId());
        task.setPickupAddress(pickupAddress);
        task.setDeliveryAddress(deliveryAddress);
        task.setStatus(Constants.DeliveryStatus.WAIT_ACCEPT);
        task.setRiderId(pickLeastLoadedRider());
        deliveryTaskMapper.insert(task);
        if (task.getRiderId() != null) {
            orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                    .eq(OrderInfo::getId, order.getId())
                    .set(OrderInfo::getRiderId, task.getRiderId()));
        }
        orderEventPublisher.publish(order.getOrderNo(), order.getUserId(), order.getMerchantId(),
                Constants.Mq.RK_DELIVERY, "订单已派单，等待骑手接单",
                Constants.NotificationType.DELIVERY, "TASK_CREATED");
        log.info("[delivery] dispatch orderNo={} riderId={}", order.getOrderNo(), task.getRiderId());
    }

    /** 我的配送任务(分页,可选 status 过滤)。 */
    public PageResult<TaskVO> tasks(Long riderId, String status, PageQuery pq) {
        Page<DeliveryTask> page = new Page<>(pq.getPage(), pq.getSize());
        LambdaQueryWrapper<DeliveryTask> qw = new LambdaQueryWrapper<DeliveryTask>()
                .eq(DeliveryTask::getRiderId, riderId);
        if (StringUtils.hasText(status)) {
            qw.eq(DeliveryTask::getStatus, status);
        }
        qw.orderByDesc(DeliveryTask::getId);
        Page<DeliveryTask> result = deliveryTaskMapper.selectPage(page, qw);
        List<TaskVO> vos = result.getRecords().stream().map(this::toVO).collect(Collectors.toList());
        return PageResult.of(vos, result.getTotal(), pq.getSize(), pq.getPage());
    }

    /** 待接单池: 未被预分配的开放任务。 */
    public List<TaskVO> available(Long riderId) {
        List<DeliveryTask> tasks = deliveryTaskMapper.selectList(new LambdaQueryWrapper<DeliveryTask>()
                .eq(DeliveryTask::getStatus, Constants.DeliveryStatus.WAIT_ACCEPT)
                .eq(DeliveryTask::getRiderId, 0L)
                .orderByAsc(DeliveryTask::getId));
        return tasks.stream().map(this::toVO).collect(Collectors.toList());
    }

    /** 抢单(条件更新防双抢): 仅 WAIT_ACCEPT 且(开放池或预分配给自己)可接。 */
    @Transactional
    public void accept(Long riderId, Long taskId) {
        DeliveryTask task = requireTask(taskId);
        int rows = deliveryTaskMapper.update(null, new LambdaUpdateWrapper<DeliveryTask>()
                .eq(DeliveryTask::getId, taskId)
                .eq(DeliveryTask::getStatus, Constants.DeliveryStatus.WAIT_ACCEPT)
                .and(w -> w.eq(DeliveryTask::getRiderId, 0L)
                        .or().eq(DeliveryTask::getRiderId, riderId))
                .set(DeliveryTask::getStatus, Constants.DeliveryStatus.ACCEPTED)
                .set(DeliveryTask::getRiderId, riderId)
                .set(DeliveryTask::getAcceptTime, LocalDateTime.now()));
        if (rows == 0) {
            throw new BizException(ResultCode.DELIVERY_GRABBED);
        }
        orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, task.getOrderId())
                .set(OrderInfo::getRiderId, riderId)
                .set(OrderInfo::getAcceptTime, LocalDateTime.now()));
        orderEventPublisher.publish(task.getOrderNo(), task.getUserId(), task.getMerchantId(),
                Constants.Mq.RK_DELIVERY, "骑手已接单，正在前往商家取餐",
                Constants.NotificationType.DELIVERY, "TASK_ACCEPTED");
    }

    /** 取餐: ACCEPTED -> PICKED。 */
    @Transactional
    public void pickup(Long riderId, Long taskId) {
        DeliveryTask task = requireOwnedTask(riderId, taskId);
        transit(task, Constants.DeliveryStatus.PICKED);
        // 原子 CAS: 带旧状态条件同时更新 status 和 picked_time;行数为 0 说明状态已被并发抢占
        int rows = deliveryTaskMapper.update(null, new LambdaUpdateWrapper<DeliveryTask>()
                .eq(DeliveryTask::getId, taskId)
                .eq(DeliveryTask::getStatus, Constants.DeliveryStatus.ACCEPTED)
                .set(DeliveryTask::getStatus, Constants.DeliveryStatus.PICKED)
                .set(DeliveryTask::getPickedTime, LocalDateTime.now()));
        if (rows == 0) {
            throw new BizException(ResultCode.DELIVERY_STATUS_INVALID);
        }
        orderEventPublisher.publish(task.getOrderNo(), task.getUserId(), task.getMerchantId(),
                Constants.Mq.RK_DELIVERY, "骑手已取餐，配送中",
                Constants.NotificationType.DELIVERY, "TASK_PICKED");
    }

    /** 送达: PICKED -> DELIVERING -> DELIVERED,并联动订单 DELIVERING -> COMPLETED。 */
    @Transactional
    public void deliver(Long riderId, Long taskId) {
        DeliveryTask task = requireOwnedTask(riderId, taskId);
        // 依次走 PICKED->DELIVERING->DELIVERED(尊重状态机)
        if (Constants.DeliveryStatus.PICKED.equals(task.getStatus())) {
            transit(task, Constants.DeliveryStatus.DELIVERING);
            int first = deliveryTaskMapper.update(null, new LambdaUpdateWrapper<DeliveryTask>()
                    .eq(DeliveryTask::getId, taskId)
                    .eq(DeliveryTask::getStatus, Constants.DeliveryStatus.PICKED)
                    .set(DeliveryTask::getStatus, Constants.DeliveryStatus.DELIVERING));
            if (first == 0) {
                throw new BizException(ResultCode.DELIVERY_STATUS_INVALID);
            }
            // 重新读取最新状态:transit 只校验不更新对象,必须用更新后的状态做第二段校验
            task = deliveryTaskMapper.selectById(taskId);
            if (task == null) {
                throw new BizException(ResultCode.DELIVERY_NO_TASK);
            }
        }
        transit(task, Constants.DeliveryStatus.DELIVERED);
        int second = deliveryTaskMapper.update(null, new LambdaUpdateWrapper<DeliveryTask>()
                .eq(DeliveryTask::getId, taskId)
                .eq(DeliveryTask::getStatus, Constants.DeliveryStatus.DELIVERING)
                .set(DeliveryTask::getStatus, Constants.DeliveryStatus.DELIVERED)
                .set(DeliveryTask::getDeliveredTime, LocalDateTime.now()));
        if (second == 0) {
            throw new BizException(ResultCode.DELIVERY_STATUS_INVALID);
        }
        // 订单 DELIVERING -> COMPLETED
        orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, task.getOrderId())
                .eq(OrderInfo::getStatus, Constants.OrderStatus.DELIVERING)
                .set(OrderInfo::getStatus, Constants.OrderStatus.COMPLETED)
                .set(OrderInfo::getDeliveredTime, LocalDateTime.now())
                .set(OrderInfo::getCompletedTime, LocalDateTime.now()));
        orderEventPublisher.publish(task.getOrderNo(), task.getUserId(), task.getMerchantId(),
                Constants.Mq.RK_DELIVERY, "订单已送达，感谢惠顾",
                Constants.NotificationType.DELIVERY, "TASK_DELIVERED");
    }

    // ---------- 内部 ----------

    /** 选择在途任务最少的骑手(预分配);无在线骑手返回 null 进入开放池。 */
    private Long pickLeastLoadedRider() {
        List<SysUser> riders = sysUserMapper.selectList(new LambdaQueryWrapper<SysUser>()
                .eq(SysUser::getRole, Constants.UserRole.RIDER)
                .last("LIMIT 50"));
        if (riders.isEmpty()) {
            return null;
        }
        Map<Long, Long> load = deliveryTaskMapper.selectList(new LambdaQueryWrapper<DeliveryTask>()
                        .in(DeliveryTask::getRiderId, riders.stream().map(SysUser::getId).collect(Collectors.toList()))
                        .in(DeliveryTask::getStatus,
                                Constants.DeliveryStatus.WAIT_ACCEPT, Constants.DeliveryStatus.ACCEPTED,
                                Constants.DeliveryStatus.PICKED, Constants.DeliveryStatus.DELIVERING))
                .stream().collect(Collectors.groupingBy(DeliveryTask::getRiderId, Collectors.counting()));
        return riders.stream()
                .min(java.util.Comparator.comparingLong(r -> load.getOrDefault(r.getId(), 0L)))
                .map(SysUser::getId)
                .orElse(null);
    }

    private void transit(DeliveryTask task, String to) {
        if (!DeliveryStateMachine.canTransit(task.getStatus(), to)) {
            throw new BizException(ResultCode.DELIVERY_STATUS_INVALID);
        }
    }

    private DeliveryTask requireTask(Long taskId) {
        DeliveryTask task = deliveryTaskMapper.selectById(taskId);
        if (task == null) {
            throw new BizException(ResultCode.DELIVERY_NO_TASK);
        }
        return task;
    }

    private DeliveryTask requireOwnedTask(Long riderId, Long taskId) {
        DeliveryTask task = requireTask(taskId);
        if (!riderId.equals(task.getRiderId())) {
            throw new BizException(ResultCode.DELIVERY_STATUS_INVALID);
        }
        return task;
    }

    private TaskVO toVO(DeliveryTask t) {
        TaskVO vo = new TaskVO();
        vo.setId(t.getId());
        vo.setOrderNo(t.getOrderNo());
        vo.setMerchantId(t.getMerchantId());
        vo.setMerchantName(merchantMapper.selectById(t.getMerchantId()) == null
                ? "店铺#" + t.getMerchantId() : merchantMapper.selectById(t.getMerchantId()).getName());
        vo.setPickupAddress(t.getPickupAddress());
        vo.setDeliveryAddress(t.getDeliveryAddress());
        vo.setStatus(t.getStatus());
        vo.setRiderId(t.getRiderId());
        vo.setCreatedAt(t.getCreatedAt());
        return vo;
    }
}
