package com.campus.service.vo;

import com.campus.common.sensitive.Sensitive;
import com.campus.common.sensitive.SensitiveType;
import lombok.Data;

/** 用户信息出参。 */
@Data
public class UserVO {

    private Long id;
    private String username;
    private String nickname;
    private String avatar;
    private String role;

    @Sensitive(SensitiveType.PHONE)
    private String phone;

    private Integer status;
}
