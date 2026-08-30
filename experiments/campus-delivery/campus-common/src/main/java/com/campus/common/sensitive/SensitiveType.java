package com.campus.common.sensitive;

import com.campus.common.util.MaskUtils;

/**
 * 脱敏类型。
 */
public enum SensitiveType {
    /** 手机号 */
    PHONE,
    /** 姓名 */
    NAME,
    /** 不脱敏 */
    NONE;

    public String mask(String raw) {
        switch (this) {
            case PHONE:
                return MaskUtils.phone(raw);
            case NAME:
                return MaskUtils.name(raw);
            default:
                return raw;
        }
    }
}
