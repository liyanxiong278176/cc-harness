package com.campus.web.controller;

import com.campus.common.api.PageResult;
import com.campus.common.api.Result;
import com.campus.common.auth.RequireRole;
import com.campus.common.auth.UserContext;
import com.campus.common.constant.Constants;
import com.campus.common.log.OperLog;
import com.campus.common.model.PageQuery;
import com.campus.service.DishService;
import com.campus.service.MerchantService;
import com.campus.service.dto.BusinessStatusReq;
import com.campus.service.dto.CategoryReq;
import com.campus.service.dto.DishReq;
import com.campus.service.dto.DishStatusReq;
import com.campus.service.dto.MerchantProfileReq;
import com.campus.service.dto.RefundReviewReq;
import com.campus.service.dto.ReviewReplyReq;
import com.campus.service.dto.StockReq;
import com.campus.service.vo.CategoryVO;
import com.campus.service.vo.DashboardVO;
import com.campus.service.vo.DishVO;
import com.campus.service.vo.MerchantVO;
import com.campus.service.vo.OrderVO;
import com.campus.service.vo.RefundVO;
import com.campus.service.vo.ReviewVO;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 商家管理端接口(docs/api.md §8),MERCHANT 角色。
 *
 * <p>已对接 campus-service 现有方法;订单受理/评价/退款相关方法为 campus-service
 * 待补充的 Service 契约(见 README 依赖说明),合并后以源码为准。</p>
 */
@RestController
@RequestMapping("/api/merchant")
@RequireRole(Constants.UserRole.MERCHANT)
public class MerchantAdminController {

    private final MerchantService merchantService;
    private final DishService dishService;

    public MerchantAdminController(MerchantService merchantService, DishService dishService) {
        this.merchantService = merchantService;
        this.dishService = dishService;
    }

    // ---------- 工作台 / 资料 / 营业状态 ----------

    @GetMapping("/dashboard")
    public Result<DashboardVO> dashboard() {
        return Result.success(merchantService.dashboard(UserContext.uid()));
    }

    @GetMapping("/profile")
    public Result<MerchantVO> myProfile() {
        return Result.success(merchantService.myProfile(UserContext.uid()));
    }

    @PutMapping("/profile")
    @OperLog(value = "更新商家资料", module = "merchant")
    public Result<MerchantVO> updateProfile(@Valid @RequestBody MerchantProfileReq req) {
        return Result.success(merchantService.updateProfile(UserContext.uid(), req));
    }

    @PutMapping("/business-status")
    @OperLog(value = "更新营业状态", module = "merchant")
    public Result<Void> setBusinessStatus(@Valid @RequestBody BusinessStatusReq req) {
        merchantService.setBusinessStatus(UserContext.uid(), req);
        return Result.success();
    }

    // ---------- 分类 ----------

    @GetMapping("/categories")
    public Result<List<CategoryVO>> categories() {
        return Result.success(merchantService.listCategories(UserContext.uid()));
    }

    @PostMapping("/categories")
    @OperLog(value = "新增分类", module = "merchant")
    public Result<CategoryVO> addCategory(@Valid @RequestBody CategoryReq req) {
        return Result.success(merchantService.addCategory(UserContext.uid(), req));
    }

    @PutMapping("/categories/{id}")
    @OperLog(value = "更新分类", module = "merchant")
    public Result<CategoryVO> updateCategory(@PathVariable Long id, @Valid @RequestBody CategoryReq req) {
        return Result.success(merchantService.updateCategory(UserContext.uid(), id, req));
    }

    @DeleteMapping("/categories/{id}")
    @OperLog(value = "删除分类", module = "merchant")
    public Result<Void> deleteCategory(@PathVariable Long id) {
        merchantService.deleteCategory(UserContext.uid(), id);
        return Result.success();
    }

    // ---------- 菜品 ----------

    @GetMapping("/dishes")
    public Result<PageResult<DishVO>> dishes(@RequestParam(required = false) Long categoryId,
                                             @RequestParam(required = false) Integer status,
                                             @Valid PageQuery pq) {
        return Result.success(dishService.pageDishes(UserContext.uid(), categoryId, status, pq));
    }

    @PostMapping("/dishes")
    @OperLog(value = "新增菜品", module = "merchant")
    public Result<DishVO> addDish(@Valid @RequestBody DishReq req) {
        return Result.success(dishService.addDish(UserContext.uid(), req));
    }

    @PutMapping("/dishes/{id}")
    @OperLog(value = "更新菜品", module = "merchant")
    public Result<DishVO> updateDish(@PathVariable Long id, @Valid @RequestBody DishReq req) {
        return Result.success(dishService.updateDish(UserContext.uid(), id, req));
    }

    @PutMapping("/dishes/{id}/stock")
    @OperLog(value = "设置库存", module = "merchant")
    public Result<Void> setStock(@PathVariable Long id, @Valid @RequestBody StockReq req) {
        dishService.setStock(UserContext.uid(), id, req);
        return Result.success();
    }

    @PutMapping("/dishes/{id}/status")
    @OperLog(value = "菜品上下架", module = "merchant")
    public Result<Void> setStatus(@PathVariable Long id, @Valid @RequestBody DishStatusReq req) {
        dishService.setStatus(UserContext.uid(), id, req);
        return Result.success();
    }

    // ---------- 订单受理(campus-service 待补充) ----------

    @GetMapping("/orders")
    public Result<PageResult<OrderVO>> orders(@RequestParam(required = false) String status,
                                              @Valid PageQuery pq) {
        return Result.success(merchantService.pageOrders(UserContext.uid(), status, pq));
    }

    @PostMapping("/orders/{orderNo}/accept")
    @OperLog(value = "商家接单", module = "merchant")
    public Result<Void> acceptOrder(@PathVariable String orderNo) {
        merchantService.acceptOrder(UserContext.uid(), orderNo);
        return Result.success();
    }

    @PostMapping("/orders/{orderNo}/ready")
    @OperLog(value = "商家出餐", module = "merchant")
    public Result<Void> readyOrder(@PathVariable String orderNo) {
        merchantService.readyOrder(UserContext.uid(), orderNo);
        return Result.success();
    }

    // ---------- 评价 / 退款(campus-service 待补充) ----------

    @GetMapping("/reviews")
    public Result<PageResult<ReviewVO>> reviews(@Valid PageQuery pq) {
        return Result.success(merchantService.listReviews(UserContext.uid(), pq));
    }

    @PostMapping("/reviews/{id}/reply")
    @OperLog(value = "回复评价", module = "merchant")
    public Result<Void> replyReview(@PathVariable Long id, @Valid @RequestBody ReviewReplyReq req) {
        merchantService.replyReview(UserContext.uid(), id, req);
        return Result.success();
    }

    @GetMapping("/refunds")
    public Result<PageResult<RefundVO>> refunds(@Valid PageQuery pq) {
        return Result.success(merchantService.listRefunds(UserContext.uid(), pq));
    }

    @PostMapping("/refunds/{id}/approve")
    @OperLog(value = "同意退款", module = "merchant")
    public Result<Void> approveRefund(@PathVariable Long id) {
        merchantService.approveRefund(UserContext.uid(), id);
        return Result.success();
    }

    @PostMapping("/refunds/{id}/reject")
    @OperLog(value = "拒绝退款", module = "merchant")
    public Result<Void> rejectRefund(@PathVariable Long id, @Valid @RequestBody RefundReviewReq req) {
        merchantService.rejectRefund(UserContext.uid(), id, req);
        return Result.success();
    }
}
