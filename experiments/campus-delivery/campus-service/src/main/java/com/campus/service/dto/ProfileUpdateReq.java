package com.campus.service.dto;

import jakarta.validation.constraints.Size;
import lombok.Data;

/** 更新个人信息请求。 */
@Data
public class ProfileUpdateReq {

    @Size(max = 50, message = "昵称最长 50")
    private String nickname;

    @Size(max = 255, message = "头像地址过长")
    private String avatar;

    private String phone;
}
