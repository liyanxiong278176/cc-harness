package com.campus.web.controller;

import com.campus.common.api.PageResult;
import com.campus.common.api.Result;
import com.campus.common.auth.UserContext;
import com.campus.common.constant.Constants;
import com.campus.common.auth.RequireRole;
import com.campus.common.log.OperLog;
import com.campus.common.model.PageQuery;
import com.campus.service.CouponService;
import com.campus.service.NotificationService;
import com.campus.service.UserService;
import com.campus.service.dto.AddressReq;
import com.campus.service.dto.ProfileUpdateReq;
import com.campus.service.vo.AddressVO;
import com.campus.service.vo.CouponVO;
import com.campus.service.vo.NotificationVO;
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
 * 用户域接口(docs/api.md §2):个人信息 / 地址 CRUD / 优惠券 / 站内通知,均需 USER 角色。
 */
@RestController
@RequestMapping("/api/user")
@RequireRole(Constants.UserRole.USER)
public class UserController {

    private final UserService userService;
    private final CouponService couponService;
    private final NotificationService notificationService;

    public UserController(UserService userService, CouponService couponService,
                          NotificationService notificationService) {
        this.userService = userService;
        this.couponService = couponService;
        this.notificationService = notificationService;
    }

    // ---------- 个人信息 ----------

    @PutMapping("/profile")
    @OperLog(value = "更新个人信息", module = "user")
    public Result<Void> updateProfile(@Valid @RequestBody ProfileUpdateReq req) {
        userService.updateProfile(UserContext.uid(), req);
        return Result.success();
    }

    // ---------- 收货地址 ----------

    @GetMapping("/addresses")
    public Result<List<AddressVO>> listAddresses() {
        return Result.success(userService.listAddresses(UserContext.uid()));
    }

    @PostMapping("/addresses")
    @OperLog(value = "新增地址", module = "user")
    public Result<AddressVO> addAddress(@Valid @RequestBody AddressReq req) {
        return Result.success(userService.addAddress(UserContext.uid(), req));
    }

    @PutMapping("/addresses/{id}")
    @OperLog(value = "更新地址", module = "user")
    public Result<AddressVO> updateAddress(@PathVariable Long id, @Valid @RequestBody AddressReq req) {
        return Result.success(userService.updateAddress(UserContext.uid(), id, req));
    }

    @DeleteMapping("/addresses/{id}")
    @OperLog(value = "删除地址", module = "user")
    public Result<Void> deleteAddress(@PathVariable Long id) {
        userService.deleteAddress(UserContext.uid(), id);
        return Result.success();
    }

    // ---------- 优惠券 ----------

    @GetMapping("/coupons")
    public Result<List<CouponVO>> myCoupons(@RequestParam(required = false) String status) {
        return Result.success(couponService.myCoupons(UserContext.uid(), status));
    }

    @PostMapping("/coupons/{couponId}/receive")
    @OperLog(value = "领取优惠券", module = "user")
    public Result<Void> receiveCoupon(@PathVariable Long couponId) {
        couponService.receive(UserContext.uid(), couponId);
        return Result.success();
    }

    // ---------- 站内通知 ----------

    @GetMapping("/notifications")
    public Result<PageResult<NotificationVO>> notifications(@Valid PageQuery pq) {
        return Result.success(notificationService.page(UserContext.uid(), pq));
    }

    @PutMapping("/notifications/{id}/read")
    public Result<Void> markRead(@PathVariable Long id) {
        notificationService.markRead(UserContext.uid(), id);
        return Result.success();
    }

    @PutMapping("/notifications/read-all")
    public Result<Void> markAllRead() {
        notificationService.markAllRead(UserContext.uid());
        return Result.success();
    }

    @GetMapping("/notifications/unread-count")
    public Result<Long> unreadCount() {
        return Result.success(notificationService.unreadCount(UserContext.uid()));
    }
}
