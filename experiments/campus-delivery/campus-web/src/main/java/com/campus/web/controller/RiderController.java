package com.campus.web.controller;

import com.campus.common.api.PageResult;
import com.campus.common.api.Result;
import com.campus.common.auth.RequireRole;
import com.campus.common.auth.UserContext;
import com.campus.common.constant.Constants;
import com.campus.common.log.OperLog;
import com.campus.common.model.PageQuery;
import com.campus.service.RiderService;
import com.campus.service.vo.TaskVO;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 骑手接口(docs/api.md §7),RIDER 角色。
 *
 * <p>Service 契约:由 campus-service 的 {@code com.campus.service.RiderService}
 * 提供(基于 delivery_task 表与 {@code DeliveryStateMachine}),方法签名如下 —
 * 请以合并后的 campus-service 源码为准。</p>
 */
@RestController
@RequestMapping("/api/rider")
@RequireRole(Constants.UserRole.RIDER)
public class RiderController {

    private final RiderService riderService;

    public RiderController(RiderService riderService) {
        this.riderService = riderService;
    }

    /** 我的配送任务:query status。 */
    @GetMapping("/tasks")
    public Result<PageResult<TaskVO>> tasks(@RequestParam(required = false) String status,
                                            @Valid PageQuery pq) {
        return Result.success(riderService.tasks(UserContext.uid(), status, pq));
    }

    /** 待接单池。 */
    @GetMapping("/tasks/available")
    public Result<List<TaskVO>> available() {
        return Result.success(riderService.available(UserContext.uid()));
    }

    /** 抢单(条件更新防双抢)。 */
    @PostMapping("/tasks/{id}/accept")
    @OperLog(value = "骑手接单", module = "rider")
    public Result<Void> accept(@PathVariable Long id) {
        riderService.accept(UserContext.uid(), id);
        return Result.success();
    }

    @PostMapping("/tasks/{id}/pickup")
    @OperLog(value = "骑手取餐", module = "rider")
    public Result<Void> pickup(@PathVariable Long id) {
        riderService.pickup(UserContext.uid(), id);
        return Result.success();
    }

    @PostMapping("/tasks/{id}/deliver")
    @OperLog(value = "骑手送达", module = "rider")
    public Result<Void> deliver(@PathVariable Long id) {
        riderService.deliver(UserContext.uid(), id);
        return Result.success();
    }
}
