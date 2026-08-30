package com.campus.common.exception;

import com.campus.common.api.ResultCode;
import lombok.Getter;

/**
 * 业务异常。全局异常处理器统一转换为 Result。
 */
@Getter
public class BizException extends RuntimeException {

    private final int code;

    public BizException(ResultCode rc) {
        super(rc.getMessage());
        this.code = rc.getCode();
    }

    public BizException(ResultCode rc, String message) {
        super(message);
        this.code = rc.getCode();
    }

    public BizException(int code, String message) {
        super(message);
        this.code = code;
    }
}
