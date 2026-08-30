package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.StockChangeLog;
import org.apache.ibatis.annotations.Mapper;

/**
 * 库存变动流水 Mapper。
 */
@Mapper
public interface StockChangeLogMapper extends BaseMapper<StockChangeLog> {
}
