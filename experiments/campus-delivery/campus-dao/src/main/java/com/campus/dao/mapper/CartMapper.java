package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.Cart;
import org.apache.ibatis.annotations.Mapper;

/**
 * 购物车 Mapper。
 */
@Mapper
public interface CartMapper extends BaseMapper<Cart> {
}
