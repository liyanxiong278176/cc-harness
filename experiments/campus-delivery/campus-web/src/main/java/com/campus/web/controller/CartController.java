package com.campus.web.controller;

import com.campus.common.api.Result;
import com.campus.common.auth.RequireRole;
import com.campus.common.auth.UserContext;
import com.campus.common.constant.Constants;
import com.campus.common.log.OperLog;
import com.campus.service.CartService;
import com.campus.service.dto.CartCheckReq;
import com.campus.service.dto.CartItemReq;
import com.campus.service.dto.CartQtyReq;
import com.campus.service.vo.CartVO;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 购物车接口(docs/api.md §4),USER 角色。
 */
@RestController
@RequestMapping("/api/cart")
@RequireRole(Constants.UserRole.USER)
public class CartController {

    private final CartService cartService;

    public CartController(CartService cartService) {
        this.cartService = cartService;
    }

    /** 按商家分组 + 合计。 */
    @GetMapping
    public Result<CartVO> cart() {
        return Result.success(cartService.cart(UserContext.uid()));
    }

    @PostMapping("/items")
    @OperLog(value = "加购", module = "cart")
    public Result<Void> addItem(@Valid @RequestBody CartItemReq req) {
        cartService.addItem(UserContext.uid(), req.getDishId(), req.getQuantity());
        return Result.success();
    }

    @PutMapping("/items/{dishId}")
    @OperLog(value = "修改购物车数量", module = "cart")
    public Result<Void> updateQuantity(@PathVariable Long dishId, @Valid @RequestBody CartQtyReq req) {
        cartService.updateQuantity(UserContext.uid(), dishId, req.getQuantity());
        return Result.success();
    }

    @PutMapping("/items/{dishId}/check")
    public Result<Void> updateChecked(@PathVariable Long dishId, @Valid @RequestBody CartCheckReq req) {
        cartService.updateChecked(UserContext.uid(), dishId, req.getChecked());
        return Result.success();
    }

    @DeleteMapping("/items/{dishId}")
    public Result<Void> removeItem(@PathVariable Long dishId) {
        cartService.removeItem(UserContext.uid(), dishId);
        return Result.success();
    }

    /** 清空(仅已勾选)。 */
    @DeleteMapping
    public Result<Void> clearChecked() {
        cartService.clearChecked(UserContext.uid());
        return Result.success();
    }
}
