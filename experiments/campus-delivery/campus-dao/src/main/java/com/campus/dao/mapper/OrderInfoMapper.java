package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.OrderInfo;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 订单主表 Mapper。
 */
@Mapper
public interface OrderInfoMapper extends BaseMapper<OrderInfo> {

    /**
     * 汇总某商家在时间区间内的实付金额(按支付时间口径,排除未支付/已取消订单)。
     *
     * @param merchantId 商家ID
     * @param from       起始时间(含)
     * @param to         结束时间(含)
     * @return 实付金额合计(无记录返回 0)
     */
    @Select("SELECT COALESCE(SUM(pay_amount), 0) FROM order_info " +
            "WHERE merchant_id = #{merchantId} " +
            "AND pay_time >= #{from} AND pay_time <= #{to} " +
            "AND status <> 'CREATED' AND status <> 'CANCELLED' AND deleted = 0")
    BigDecimal sumPayAmount(@Param("merchantId") Long merchantId,
                            @Param("from") LocalDateTime from,
                            @Param("to") LocalDateTime to);
}
