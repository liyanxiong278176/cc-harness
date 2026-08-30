package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.common.util.MoneyUtils;
import com.campus.dao.entity.Cart;
import com.campus.dao.entity.Dish;
import com.campus.dao.entity.Merchant;
import com.campus.dao.mapper.CartMapper;
import com.campus.dao.mapper.DishMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.service.vo.CartItemVO;
import com.campus.service.vo.CartVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 购物车服务(按商家分组)。
 */
@Service
public class CartService {

    private final CartMapper cartMapper;
    private final DishMapper dishMapper;
    private final MerchantMapper merchantMapper;

    public CartService(CartMapper cartMapper, DishMapper dishMapper, MerchantMapper merchantMapper) {
        this.cartMapper = cartMapper;
        this.dishMapper = dishMapper;
        this.merchantMapper = merchantMapper;
    }

    public CartVO cart(Long userId) {
        List<Cart> rows = cartMapper.selectList(new LambdaQueryWrapper<Cart>()
                .eq(Cart::getUserId, userId)
                .orderByAsc(Cart::getId));
        Map<Long, CartVO.CartGroup> groups = new LinkedHashMap<>();
        BigDecimal total = BigDecimal.ZERO.setScale(MoneyUtils.SCALE);
        int checkedCount = 0;
        for (Cart row : rows) {
            Dish dish = dishMapper.selectById(row.getDishId());
            if (dish == null || dish.getStatus() == null || dish.getStatus() != 1) {
                continue;
            }
            CartVO.CartGroup group = groups.computeIfAbsent(row.getMerchantId(), mid -> {
                Merchant m = merchantMapper.selectById(mid);
                CartVO.CartGroup g = new CartVO.CartGroup();
                g.setMerchantId(mid);
                g.setMerchantName(m == null ? "店铺#" + mid : m.getName());
                g.setIsOpen(m == null ? 0 : m.getIsOpen());
                g.setItems(new ArrayList<>());
                g.setGoodsAmount(BigDecimal.ZERO.setScale(MoneyUtils.SCALE));
                return g;
            });
            CartItemVO item = new CartItemVO();
            item.setDishId(dish.getId());
            item.setDishName(dish.getName());
            item.setImage(dish.getImage());
            item.setPrice(dish.getPrice());
            item.setQuantity(row.getQuantity());
            item.setChecked(row.getChecked());
            item.setStock(dish.getStock());
            group.getItems().add(item);
            BigDecimal line = MoneyUtils.multiply(dish.getPrice(), BigDecimal.valueOf(row.getQuantity()));
            if (row.getChecked() != null && row.getChecked() == 1) {
                group.setGoodsAmount(MoneyUtils.add(group.getGoodsAmount(), line));
                total = MoneyUtils.add(total, line);
                checkedCount += row.getQuantity();
            }
        }
        CartVO vo = new CartVO();
        vo.setGroups(new ArrayList<>(groups.values()));
        vo.setTotalAmount(total);
        vo.setTotalCheckedCount(checkedCount);
        return vo;
    }

    @Transactional
    public void addItem(Long userId, Long dishId, int quantity) {
        Dish dish = dishMapper.selectById(dishId);
        if (dish == null) {
            throw new BizException(ResultCode.DISH_NOT_FOUND);
        }
        if (dish.getStatus() == null || dish.getStatus() != 1) {
            throw new BizException(ResultCode.DISH_OFF_SALE);
        }
        Cart exist = cartMapper.selectOne(new LambdaQueryWrapper<Cart>()
                .eq(Cart::getUserId, userId)
                .eq(Cart::getDishId, dishId)
                .last("LIMIT 1"));
        if (exist != null) {
            int newQty = exist.getQuantity() + quantity;
            if (newQty > dish.getStock()) {
                throw new BizException(ResultCode.STOCK_NOT_ENOUGH);
            }
            Cart upd = new Cart();
            upd.setId(exist.getId());
            upd.setQuantity(newQty);
            cartMapper.updateById(upd);
        } else {
            if (quantity > dish.getStock()) {
                throw new BizException(ResultCode.STOCK_NOT_ENOUGH);
            }
            Cart row = new Cart();
            row.setUserId(userId);
            row.setMerchantId(dish.getMerchantId());
            row.setDishId(dishId);
            row.setQuantity(quantity);
            row.setChecked(1);
            cartMapper.insert(row);
        }
    }

    @Transactional
    public void updateQuantity(Long userId, Long dishId, int quantity) {
        Cart exist = requireRow(userId, dishId);
        Dish dish = dishMapper.selectById(dishId);
        if (quantity > (dish == null ? 0 : dish.getStock())) {
            throw new BizException(ResultCode.STOCK_NOT_ENOUGH);
        }
        Cart upd = new Cart();
        upd.setId(exist.getId());
        upd.setQuantity(quantity);
        cartMapper.updateById(upd);
    }

    @Transactional
    public void updateChecked(Long userId, Long dishId, int checked) {
        Cart exist = requireRow(userId, dishId);
        Cart upd = new Cart();
        upd.setId(exist.getId());
        upd.setChecked(checked == 1 ? 1 : 0);
        cartMapper.updateById(upd);
    }

    @Transactional
    public void removeItem(Long userId, Long dishId) {
        requireRow(userId, dishId);
        cartMapper.delete(new LambdaQueryWrapper<Cart>()
                .eq(Cart::getUserId, userId)
                .eq(Cart::getDishId, dishId));
    }

    @Transactional
    public void clearChecked(Long userId) {
        cartMapper.delete(new LambdaQueryWrapper<Cart>()
                .eq(Cart::getUserId, userId)
                .eq(Cart::getChecked, 1));
    }

    private Cart requireRow(Long userId, Long dishId) {
        Cart row = cartMapper.selectOne(new LambdaQueryWrapper<Cart>()
                .eq(Cart::getUserId, userId)
                .eq(Cart::getDishId, dishId)
                .last("LIMIT 1"));
        if (row == null) {
            throw new BizException(ResultCode.CART_EMPTY);
        }
        return row;
    }
}
