package com.campus.service.support;

import com.campus.common.util.AesUtils;
import com.campus.dao.entity.SysUser;
import com.campus.service.vo.UserVO;

/**
 * 用户实体 -> 出参转换(手机号解密,出参脱敏由 @Sensitive 序列化器处理)。
 */
public final class UserConverter {

    private UserConverter() {
    }

    public static UserVO toVO(SysUser user) {
        if (user == null) {
            return null;
        }
        UserVO vo = new UserVO();
        vo.setId(user.getId());
        vo.setUsername(user.getUsername());
        vo.setNickname(user.getNickname());
        vo.setAvatar(user.getAvatar());
        vo.setRole(user.getRole());
        vo.setStatus(user.getStatus());
        String phone = AesUtils.decrypt(user.getPhone());
        vo.setPhone(phone);
        return vo;
    }
}
