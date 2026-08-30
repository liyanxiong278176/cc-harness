package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.api.ResultCode;
import com.campus.common.exception.BizException;
import com.campus.common.model.PageQuery;
import com.campus.dao.entity.Notification;
import com.campus.dao.mapper.NotificationMapper;
import com.campus.service.vo.NotificationVO;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 站内通知服务。创建按 (user_id, biz_type, biz_id) 幂等(唯一键兜底)。
 */
@Service
public class NotificationService {

    private static final Logger log = LoggerFactory.getLogger(NotificationService.class);

    private final NotificationMapper notificationMapper;

    public NotificationService(NotificationMapper notificationMapper) {
        this.notificationMapper = notificationMapper;
    }

    /**
     * 创建通知(幂等: 相同 user+bizType+bizId 只落一条)。
     */
    @Transactional
    public void create(Long userId, String type, String title, String content,
                       String bizType, String bizId) {
        Long exist = notificationMapper.selectCount(new LambdaQueryWrapper<Notification>()
                .eq(Notification::getUserId, userId)
                .eq(Notification::getBizType, bizType)
                .eq(Notification::getBizId, bizId));
        if (exist != null && exist > 0) {
            return;
        }
        Notification n = new Notification();
        n.setUserId(userId);
        n.setType(type);
        n.setTitle(title);
        n.setContent(content == null ? "" : content);
        n.setBizType(bizType == null ? "" : bizType);
        n.setBizId(bizId == null ? "" : bizId);
        n.setIsRead(0);
        try {
            notificationMapper.insert(n);
        } catch (DuplicateKeyException e) {
            log.debug("notification dup ignored: user={} bizType={} bizId={}", userId, bizType, bizId);
        }
    }

    public PageResult<NotificationVO> page(Long userId, PageQuery pq) {
        Page<Notification> page = new Page<>(pq.getPage(), pq.getSize());
        Page<Notification> result = notificationMapper.selectPage(page,
                new LambdaQueryWrapper<Notification>()
                        .eq(Notification::getUserId, userId)
                        .orderByDesc(Notification::getId));
        return PageResult.of(result.getRecords().stream()
                .map(NotificationService::toVO).collect(Collectors.toList()),
                result.getTotal(), pq.getSize(), pq.getPage());
    }

    public List<NotificationVO> latest(Long userId, int limit) {
        List<Notification> list = notificationMapper.selectList(new LambdaQueryWrapper<Notification>()
                .eq(Notification::getUserId, userId)
                .orderByDesc(Notification::getId)
                .last("LIMIT " + Math.max(1, Math.min(limit, 50))));
        return list.stream().map(NotificationService::toVO).collect(Collectors.toList());
    }

    public long unreadCount(Long userId) {
        Long c = notificationMapper.selectCount(new LambdaQueryWrapper<Notification>()
                .eq(Notification::getUserId, userId)
                .eq(Notification::getIsRead, 0));
        return c == null ? 0 : c;
    }

    @Transactional
    public void markRead(Long userId, Long id) {
        Notification n = notificationMapper.selectById(id);
        if (n == null || !n.getUserId().equals(userId)) {
            throw new BizException(ResultCode.NOTIFICATION_NOT_FOUND);
        }
        Notification upd = new Notification();
        upd.setId(id);
        upd.setIsRead(1);
        notificationMapper.updateById(upd);
    }

    @Transactional
    public void markAllRead(Long userId) {
        notificationMapper.markAllRead(userId);
    }

    public static NotificationVO toVO(Notification n) {
        NotificationVO vo = new NotificationVO();
        vo.setId(n.getId());
        vo.setType(n.getType());
        vo.setTitle(n.getTitle());
        vo.setContent(n.getContent());
        vo.setBizType(n.getBizType());
        vo.setBizId(n.getBizId());
        vo.setIsRead(n.getIsRead());
        vo.setCreatedAt(n.getCreatedAt());
        return vo;
    }
}
