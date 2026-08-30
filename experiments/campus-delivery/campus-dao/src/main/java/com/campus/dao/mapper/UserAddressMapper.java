package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.UserAddress;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Update;

/**
 * 用户收货地址 Mapper。
 */
@Mapper
public interface UserAddressMapper extends BaseMapper<UserAddress> {

    /**
     * 将某用户的所有默认地址置为非默认(设置新默认地址前调用)。
     *
     * @param userId 用户ID
     * @return 影响行数
     */
    @Update("UPDATE user_address SET is_default = 0 " +
            "WHERE user_id = #{userId} AND is_default = 1 AND deleted = 0")
    int clearDefault(@Param("userId") Long userId);
}
