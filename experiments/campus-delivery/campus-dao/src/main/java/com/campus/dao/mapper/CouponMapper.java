package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.Coupon;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Update;

/**
 * 优惠券模板 Mapper。
 */
@Mapper
public interface CouponMapper extends BaseMapper<Coupon> {

    /**
     * 增加已发行量(条件更新,防超发):
     * 仅当已发行量 + delta 不超过发行总量时更新。
     *
     * @param id         券模板ID
     * @param delta      增加数量(通常为 1)
     * @param totalCount 发行总量上限
     * @return 影响行数(0 表示超发)
     */
    @Update("UPDATE coupon SET issued_count = issued_count + #{delta}, " +
            "version = version + 1 " +
            "WHERE id = #{id} AND issued_count + #{delta} <= #{totalCount} AND deleted = 0")
    int incrementIssued(@Param("id") Long id, @Param("delta") int delta, @Param("totalCount") int totalCount);
}
