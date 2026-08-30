package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.OrderItem;
import org.apache.ibatis.annotations.Mapper;

/**
 * 订单明细 Mapper。
 */
@Mapper
public interface OrderItemMapper extends BaseMapper<OrderItem> {
}
