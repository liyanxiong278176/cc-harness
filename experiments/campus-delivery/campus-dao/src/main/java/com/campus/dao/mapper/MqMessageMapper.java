package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.MqMessage;
import org.apache.ibatis.annotations.Mapper;

/**
 * 本地消息表(outbox) Mapper。
 */
@Mapper
public interface MqMessageMapper extends BaseMapper<MqMessage> {
}
