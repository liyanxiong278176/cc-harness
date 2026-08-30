package com.campus.service.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Data;

/** 登录请求。 */
@Data
public class LoginReq {

    @NotBlank(message = "用户名不能为空")
    private String username;

    @NotBlank(message = "密码不能为空")
    private String password;
}
