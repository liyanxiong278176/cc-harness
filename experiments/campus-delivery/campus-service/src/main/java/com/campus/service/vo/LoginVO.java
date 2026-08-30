package com.campus.service.vo;

import lombok.Data;

/** 登录/注册出参。 */
@Data
public class LoginVO {

    private String token;
    private UserVO user;
}
