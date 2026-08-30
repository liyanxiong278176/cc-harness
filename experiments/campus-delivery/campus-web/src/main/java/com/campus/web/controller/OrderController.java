package com.campus.web.controller;

import com.campus.common.api.PageResult;
import com.campus.common.api.Result;
import com.campus.common.auth.RequireRole;
import com.campus.common.auth.UserContext;
import com.campus.common.constant.Constants;
import com.campus.common.log.OperLog;
import com.campus.common.model.PageQuery;
import com.campus.service.OrderService;
import com.campus.service.PaymentService;
import com.campus.service.dto.CancelReq;
import com.campus.service.dto.CheckoutReq;
import com.campus.service.dto.PayReq;
import com.campus.service.dto.RefundReq;
import com.campus.service.dto.ReviewCreateReq;
import com.campus.service.vo.OrderDetailVO;
import com.campus.service.vo.OrderVO;
import com.campus.service.vo.TrackVO;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 订单接口(docs/api.md §5),USER 角色。
 */
@RestController
@RequestMapping("/api/orders")
@RequireRole(Constants.UserRole.USER)
public class OrderController {

    private final OrderService orderService;
    private final PaymentService paymentService;

    public OrderController(OrderService orderService, PaymentService paymentService) {
        this.orderService = orderService;
        this.paymentService = paymentService;
    }

    /** 结算下单:使用该商家已勾选购物车行,返回 {orderNo}。 */
    @PostMapping("/checkout")
    @OperLog(value = "结算下单", module = "order")
    public Result<String> checkout(@Valid @RequestBody CheckoutReq req) {
        return Result.success(orderService.checkout(UserContext.uid(), req));
    }

    @GetMapping
    public Result<PageResult<OrderVO>> page(@RequestParam(required = false) String status,
                                            @Valid PageQuery pq) {
        return Result.success(orderService.pageOrders(UserContext.uid(), status, pq));
    }

    @GetMapping("/{orderNo}")
    public Result<OrderDetailVO> detail(@PathVariable String orderNo) {
        return Result.success(orderService.detail(UserContext.uid(), orderNo));
    }

    /** 取消订单(仅 CREATED)。 */
    @PostMapping("/{orderNo}/cancel")
    @OperLog(value = "取消订单", module = "order")
    public Result<Void> cancel(@PathVariable String orderNo, @Valid @RequestBody CancelReq req) {
        orderService.cancel(UserContext.uid(), orderNo, req.getReason());
        return Result.success();
    }

    /** 发起支付,返回 {paymentNo,payParams}。 */
    @PostMapping("/{orderNo}/pay")
    @OperLog(value = "发起支付", module = "order")
    public Result<Map<String, String>> pay(@PathVariable String orderNo, @Valid @RequestBody PayReq req) {
        return Result.success(paymentService.pay(UserContext.uid(), orderNo, req.getChannel()));
    }

    /** 申请退款(仅 PAID/PREPARING/DELIVERING)。由 campus-service 的 OrderService 提供。 */
    @PostMapping("/{orderNo}/refund")
    @OperLog(value = "申请退款", module = "order")
    public Result<Void> refund(@PathVariable String orderNo, @Valid @RequestBody RefundReq req) {
        orderService.applyRefund(UserContext.uid(), orderNo, req);
        return Result.success();
    }

    /** 完成订单评价(仅 COMPLETED 可评)。由 campus-service 的 OrderService 提供。 */
    @PostMapping("/{orderNo}/review")
    @OperLog(value = "评价订单", module = "order")
    public Result<Void> review(@PathVariable String orderNo, @Valid @RequestBody ReviewCreateReq req) {
        orderService.createReview(UserContext.uid(), orderNo, req);
        return Result.success();
    }

    /** 订单+支付+配送状态跟踪。 */
    @GetMapping("/{orderNo}/track")
    public Result<TrackVO> track(@PathVariable String orderNo) {
        return Result.success(orderService.track(UserContext.uid(), orderNo));
    }
}
