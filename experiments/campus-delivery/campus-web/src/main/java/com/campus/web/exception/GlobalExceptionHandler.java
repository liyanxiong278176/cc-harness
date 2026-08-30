package com.campus.web.exception;

import com.campus.common.api.Result;
import com.campus.common.api.ResultCode;
import com.campus.common.exception.BizException;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.ConstraintViolationException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.validation.BindException;
import org.springframework.validation.FieldError;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.stream.Collectors;

/**
 * 全局异常处理(统一返回 {@link Result<T>}):
 * <ul>
 *   <li>{@link BizException} → 业务错误码(原样透传 code/message);</li>
 *   <li>参数校验异常(@RequestBody/@RequestParam/query 绑定)→ BAD_PARAM(40000);</li>
 *   <li>请求体/格式异常 → PARAM_FORMAT(40001);</li>
 *   <li>其余异常 → 兜底 INTERNAL_ERROR(50000),并记录错误日志。</li>
 * </ul>
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(BizException.class)
    public Result<Void> handleBizException(BizException e) {
        return Result.fail(e.getCode(), e.getMessage());
    }

    /** @RequestBody 上的 @Valid 校验失败。 */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public Result<Void> handleMethodArgumentNotValid(MethodArgumentNotValidException e) {
        String msg = e.getBindingResult().getFieldErrors().stream()
                .map(FieldError::getDefaultMessage)
                .filter(m -> m != null && !m.isBlank())
                .collect(Collectors.joining("; "));
        return Result.fail(ResultCode.BAD_PARAM, msg.isBlank() ? ResultCode.BAD_PARAM.getMessage() : msg);
    }

    /** @Validated 方法参数(如 @RequestParam 上的约束)校验失败。 */
    @ExceptionHandler(ConstraintViolationException.class)
    public Result<Void> handleConstraintViolation(ConstraintViolationException e) {
        String msg = e.getConstraintViolations().stream()
                .map(ConstraintViolation::getMessage)
                .collect(Collectors.joining("; "));
        return Result.fail(ResultCode.BAD_PARAM, msg.isBlank() ? ResultCode.BAD_PARAM.getMessage() : msg);
    }

    /** query/表单对象绑定失败。 */
    @ExceptionHandler(BindException.class)
    public Result<Void> handleBindException(BindException e) {
        String msg = e.getBindingResult().getFieldErrors().stream()
                .map(FieldError::getDefaultMessage)
                .filter(m -> m != null && !m.isBlank())
                .collect(Collectors.joining("; "));
        return Result.fail(ResultCode.BAD_PARAM, msg.isBlank() ? ResultCode.BAD_PARAM.getMessage() : msg);
    }

    @ExceptionHandler(MissingServletRequestParameterException.class)
    public Result<Void> handleMissingParam(MissingServletRequestParameterException e) {
        return Result.fail(ResultCode.BAD_PARAM, "缺少参数: " + e.getParameterName());
    }

    /** 请求体缺失/JSON 解析失败。 */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public Result<Void> handleNotReadable(HttpMessageNotReadableException e) {
        log.debug("[exception] request body not readable: {}", e.getMessage());
        return Result.fail(ResultCode.PARAM_FORMAT);
    }

    @ExceptionHandler(HttpMediaTypeNotSupportedException.class)
    public Result<Void> handleMediaType(HttpMediaTypeNotSupportedException e) {
        return Result.fail(ResultCode.BAD_PARAM, "不支持的 Content-Type: " + e.getContentType());
    }

    /** 兜底: 未预期的异常统一返回内部错误,细节仅记录日志。 */
    @ExceptionHandler(Exception.class)
    public Result<Void> handleOther(Exception e) {
        log.error("[exception] unhandled error", e);
        return Result.fail(ResultCode.INTERNAL_ERROR);
    }
}
