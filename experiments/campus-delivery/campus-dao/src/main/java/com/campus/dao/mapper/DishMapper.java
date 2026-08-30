package com.campus.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.dao.entity.Dish;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Update;

/**
 * 菜品 Mapper。
 * 库存扣减/回滚使用条件更新(防超卖),库存条件由调用方先读取实体 version 后代入。
 */
@Mapper
public interface DishMapper extends BaseMapper<Dish> {

    /**
     * 条件扣减库存(防超卖):
     * 仅当库存充足且 version 匹配时更新,同时自增 version(乐观锁)。
     *
     * @param id      菜品ID
     * @param qty     扣减数量
     * @param version 期望的乐观锁版本
     * @return 影响行数(0 表示库存不足或版本冲突)
     */
    @Update("UPDATE dish SET stock = stock - #{qty}, " +
            "sold_count = sold_count + #{qty}, " +
            "version = version + 1 " +
            "WHERE id = #{id} AND stock >= #{qty} AND version = #{version} AND deleted = 0")
    int deductStock(@Param("id") Long id, @Param("qty") int qty, @Param("version") Long version);

    /**
     * 回滚库存(取消/退款): 仅当已售数量足够时回退,同时自增 version。
     *
     * @param id  菜品ID
     * @param qty 回滚数量
     * @return 影响行数
     */
    @Update("UPDATE dish SET stock = stock + #{qty}, " +
            "sold_count = sold_count - #{qty}, " +
            "version = version + 1 " +
            "WHERE id = #{id} AND sold_count >= #{qty} AND deleted = 0")
    int rollbackStock(@Param("id") Long id, @Param("qty") int qty);
}
