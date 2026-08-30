package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.Notification;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Update;

/**
 * 站内通知 Mapper。
 */
@Mapper
public interface NotificationMapper extends BaseMapper<Notification> {

    /**
     * 将某用户全部未读通知置为已读(标记已读时间)。
     *
     * @param userId 用户ID
     * @return 影响行数
     */
    @Update("UPDATE notification SET is_read = 1, read_at = NOW() " +
            "WHERE user_id = #{userId} AND is_read = 0 AND deleted = 0")
    int markAllRead(@Param("userId") Long userId);
}
