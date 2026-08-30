package com.campus.web.controller;

import com.campus.common.api.Result;
import com.campus.service.PaymentService;
import com.campus.service.dto.PaymentNotifyReq;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 模拟支付接口(docs/api.md §6):mock 渠道回调(服务内部/可手动,WebConfig 放行鉴权)。
 */
@RestController
@RequestMapping("/api/payment")
public class PaymentController {

    private final PaymentService paymentService;

    public PaymentController(PaymentService paymentService) {
        this.paymentService = paymentService;
    }

    /** 模拟渠道回调(幂等:trade_no 唯一 + dedup 键 + FOR UPDATE)。 */
    @PostMapping("/mock/notify")
    public Result<Map<String, Object>> mockNotify(@Valid @RequestBody PaymentNotifyReq req) {
        return Result.success(paymentService.notify(req));
    }

    /** 手动测试入口:query orderNo,success。 */
    @GetMapping("/mock/notify")
    public Result<Map<String, Object>> mockNotifyGet(
            @RequestParam String orderNo,
            @RequestParam(required = false) Boolean success,
            @RequestParam(required = false) String channel) {
        PaymentNotifyReq req = new PaymentNotifyReq();
        req.setOrderNo(orderNo);
        req.setSuccess(success == null ? Boolean.TRUE : success);
        req.setChannel(channel);
        return Result.success(paymentService.notify(req));
    }
}
