package com.campus.web.controller;

import com.campus.common.api.Result;
import com.campus.common.auth.UserContext;
import com.campus.common.log.OperLog;
import com.campus.service.AuthService;
import com.campus.service.dto.LoginReq;
import com.campus.service.dto.PasswordChangeReq;
import com.campus.service.dto.RegisterReq;
import com.campus.service.vo.LoginVO;
import com.campus.service.vo.UserVO;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 认证接口(docs/api.md §1):/auth/register、/auth/login 公开,其余需登录。
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    /** 注册(公开,角色固定 USER),返回 {token,user}。 */
    @PostMapping("/register")
    public Result<LoginVO> register(@Valid @RequestBody RegisterReq req) {
        return Result.success(authService.register(req));
    }

    /** 登录(公开),返回 {token,user}。 */
    @PostMapping("/login")
    public Result<LoginVO> login(@Valid @RequestBody LoginReq req) {
        return Result.success(authService.login(req));
    }

    /** 当前登录用户(手机号脱敏)。 */
    @GetMapping("/me")
    public Result<UserVO> me() {
        return Result.success(authService.me(UserContext.uid()));
    }

    /** 修改密码。 */
    @PutMapping("/password")
    @OperLog(value = "修改密码", module = "auth")
    public Result<Void> changePassword(@Valid @RequestBody PasswordChangeReq req) {
        authService.changePassword(UserContext.uid(), req);
        return Result.success();
    }
}
