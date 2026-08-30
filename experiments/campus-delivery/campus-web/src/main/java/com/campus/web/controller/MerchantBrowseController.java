package com.campus.web.controller;

import com.campus.common.api.PageResult;
import com.campus.common.api.Result;
import com.campus.common.model.PageQuery;
import com.campus.service.DishService;
import com.campus.service.MerchantService;
import com.campus.service.vo.MenuVO;
import com.campus.service.vo.MerchantVO;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 商家浏览接口(docs/api.md §3):公开/登录即可访问,无需鉴权(WebConfig 放行)。
 */
@RestController
@RequestMapping("/api/merchants")
public class MerchantBrowseController {

    private final MerchantService merchantService;
    private final DishService dishService;

    public MerchantBrowseController(MerchantService merchantService, DishService dishService) {
        this.merchantService = merchantService;
        this.dishService = dishService;
    }

    /** 商家列表:query zone,page,size;营业中优先。 */
    @GetMapping
    public Result<PageResult<MerchantVO>> page(@RequestParam(required = false) String zone,
                                               @Valid PageQuery pq) {
        return Result.success(merchantService.page(zone, pq));
    }

    /** 商家详情。 */
    @GetMapping("/{id}")
    public Result<MerchantVO> detail(@PathVariable Long id) {
        return Result.success(merchantService.detail(id));
    }

    /** 分类 + 上架菜品(Redis 缓存)。 */
    @GetMapping("/{id}/menu")
    public Result<MenuVO> menu(@PathVariable Long id) {
        return Result.success(dishService.menu(id));
    }
}
