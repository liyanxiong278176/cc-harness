package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.UserCoupon;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Update;

/**
 * 用户优惠券 Mapper。
 */
@Mapper
public interface UserCouponMapper extends BaseMapper<UserCoupon> {

    /**
     * 核销用户券(条件更新 UNUSED -> USED):
     * 仅当券处于 UNUSED 且 version 匹配时更新,同时自增 version(防并发重复核销)。
     *
     * @param id      用户券ID
     * @param orderId 核销订单ID
     * @param version 期望的乐观锁版本
     * @return 影响行数(0 表示已核销或版本冲突)
     */
    @Update("UPDATE user_coupon SET status = 'USED', " +
            "used_order_id = #{orderId}, " +
            "used_at = NOW(), " +
            "version = version + 1 " +
            "WHERE id = #{id} AND status = 'UNUSED' AND version = #{version} AND deleted = 0")
    int markUsed(@Param("id") Long id, @Param("orderId") Long orderId, @Param("version") Long version);

    /**
     * 退还用户券(USED -> UNUSED): 按用户 + 核销订单定位,取消/退款时调用。
     *
     * @param orderId 核销订单ID
     * @param userId  用户ID
     * @return 影响行数
     */
    @Update("UPDATE user_coupon SET status = 'UNUSED', " +
            "used_order_id = 0, " +
            "used_at = NULL, " +
            "version = version + 1 " +
            "WHERE user_id = #{userId} AND used_order_id = #{orderId} AND status = 'USED' AND deleted = 0")
    int release(@Param("orderId") Long orderId, @Param("userId") Long userId);
}
