"""Safe, structured error contracts for model and SSE streaming.

The provider stream and the browser SSE connection are presentation paths, not
durable state stores.  This module gives both paths the same small error
vocabulary without copying provider payloads, headers, stack traces, or tool
arguments into the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .run_store import RunStoreError


_TRANSPORT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_AUTH_STATUS_CODES = {401, 403}
_CONFIG_STATUS_CODES = {400, 404, 413, 422}


def _safe_request_id(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:128]
    return None


def _request_id_from(exc: BaseException) -> str | None:
    for name in ("request_id", "response_id"):
        found = _safe_request_id(getattr(exc, name, None))
        if found:
            return found
    response = getattr(exc, "response", None)
    if response is not None:
        for name in ("request_id", "response_id"):
            found = _safe_request_id(getattr(response, name, None))
            if found:
                return found
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            for name in ("x-request-id", "request-id"):
                found = _safe_request_id(headers.get(name))
                if found:
                    return found
    return None


def _transport_like(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in _TRANSPORT_STATUS_CODES:
        return True
    names: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(names) < 8:
        names.append(type(current).__name__.casefold())
        current = current.__cause__ or current.__context__
    text = str(exc).casefold()
    return any(name in {"readtimeout", "read_time_out", "readtimedout", "apiconnectionerror", "apitimeouterror", "remoteprotocolerror"} for name in names) or any(
        marker in text
        for marker in (
            "read timeout",
            "read timed out",
            "connection reset",
            "connection closed",
            "connection aborted",
            "incomplete read",
            "incomplete chunked read",
            "remoteprotocolerror",
            "server disconnected",
            "temporarily unavailable",
            "status code 429",
            "status code 500",
            "status code 502",
            "status code 503",
            "status code 504",
        )
    )


@dataclass(frozen=True)
class StreamErrorDetails:
    """The browser-safe error envelope shared by REST, SSE, and the Runtime."""

    code: str
    phase: str
    retryable: bool
    partial_output: bool = False
    attempt: int | None = None
    retry_after: float | None = None
    next_action: str = "从最近检查点继续"
    message: str = "运行时发生错误"
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "phase": self.phase,
            "retryable": self.retryable,
            "partial_output": self.partial_output,
            "attempt": self.attempt,
            "retry_after": self.retry_after,
            "next_action": self.next_action,
            "message": self.message,
        }
        if self.request_id:
            value["request_id"] = self.request_id
        return value


class ProviderStreamError(RuntimeError):
    """A transport stream ended after a retry-safe boundary was crossed.

    The original exception is retained for local diagnostics only.  ``str`` is
    intentionally generic so accidental logging in a browser-facing path
    cannot expose provider response bodies or credentials.
    """

    code = "provider_stream_interrupted"

    def __init__(
        self,
        cause: BaseException,
        *,
        attempts: int,
        partial_output: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__("provider stream interrupted")
        self.cause = cause
        self.attempts = max(1, int(attempts))
        self.partial_output = bool(partial_output)
        self.request_id = request_id or _request_id_from(cause)


def classify_stream_error(
    exc: BaseException,
    *,
    partial_output: bool = False,
    attempt: int | None = None,
    phase: str | None = None,
) -> StreamErrorDetails:
    """Map an exception to a bounded, actionable error contract.

    ``partial_output`` is a safety boundary: once a visible model/tool delta
    has been emitted, no automatic provider retry is considered safe.
    """

    if isinstance(exc, ProviderStreamError):
        cause = exc.cause
        partial_output = exc.partial_output
        attempt = exc.attempts
        request_id = exc.request_id or _request_id_from(cause)
    else:
        cause = exc
        request_id = _request_id_from(exc)

    code = str(getattr(cause, "code", "") or "")
    status = getattr(cause, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    name = type(cause).__name__.casefold()
    text = str(cause).casefold()

    if (
        code == "provider_protocol_error"
        or name in {"providerprotocolerror", "kernelprotocolerror"}
        or "reasoning_content" in text
        or "tool arguments" in text
        or "malformed tool" in text
    ):
        return StreamErrorDetails(
            code="provider_protocol_error",
            phase=phase or "model",
            retryable=False,
            partial_output=partial_output,
            attempt=attempt,
            next_action="检查模型协议、thinking 配置或切换模型后，再从当前检查点继续",
            message="模型返回的数据格式无法安全重放",
            request_id=request_id,
        )
    if ("timeout" in name or "timed out" in text) and not _transport_like(cause):
        return StreamErrorDetails(
            code="model_timeout",
            phase=phase or "model",
            retryable=not partial_output,
            partial_output=partial_output,
            attempt=attempt,
            next_action="检查模型响应耗时；保留已生成内容后从当前检查点继续",
            message="模型响应超时，已保留当前运行边界",
            request_id=request_id,
        )
    if status in _AUTH_STATUS_CODES or any(marker in text for marker in ("invalid api key", "authentication", "unauthorized", "forbidden")):
        return StreamErrorDetails(
            code="provider_auth_error",
            phase=phase or "connection",
            retryable=False,
            partial_output=partial_output,
            attempt=attempt,
            next_action="检查 Base URL、API Key 和模型权限后，从当前检查点继续",
            message="模型连接凭据或权限无效",
            request_id=request_id,
        )
    if status in _CONFIG_STATUS_CODES or any(marker in text for marker in ("model not found", "invalid model", "unsupported parameter", "bad request")):
        return StreamErrorDetails(
            code="provider_config_error",
            phase=phase or "model",
            retryable=False,
            partial_output=partial_output,
            attempt=attempt,
            next_action="检查模型名称、Base URL 和请求参数后，从当前检查点继续",
            message="模型配置或请求参数不被提供方接受",
            request_id=request_id,
        )
    if "imageunsupported" in name or "image attachment" in text or "vision not supported" in text:
        return StreamErrorDetails(
            code="provider_config_error",
            phase=phase or "model",
            retryable=False,
            partial_output=partial_output,
            attempt=attempt,
            next_action="切换支持图片输入的模型，或移除图片后从当前检查点继续",
            message="当前模型不支持图片输入",
            request_id=request_id,
        )
    if _transport_like(cause):
        return StreamErrorDetails(
            code="provider_transport_error",
            phase=phase or "connection",
            retryable=not partial_output,
            partial_output=partial_output,
            attempt=attempt,
            retry_after=1.0 if not partial_output else None,
            next_action=(
                "连接恢复后继续；不要自动重发已产生输出的本轮"
                if partial_output
                else "连接恢复后可重试本轮，或从当前检查点继续"
            ),
            message=(
                "模型连接在回复过程中中断，已冻结未提交内容"
                if partial_output
                else "模型连接暂时不可用，Runtime 将有限重试"
            ),
            request_id=request_id,
        )
    if isinstance(cause, RunStoreError) or any(
        marker in name
        for marker in ("runstore", "sequenceconflict", "leasefence", "projection", "digest")
    ) or any(
        marker in text
        for marker in ("snapshot digest", "projection cursor", "event rebuild", "stored projection")
    ):
        return StreamErrorDetails(
            code="persistence_error",
            phase=phase or "persistence",
            retryable=False,
            partial_output=partial_output,
            attempt=attempt,
            next_action="停止新的副作用动作，修复或重建投影后从原检查点继续",
            message="运行状态落盘或投影校验失败，已暂停新的动作",
            request_id=request_id,
        )
    return StreamErrorDetails(
        code="runtime_error",
        phase=phase or "model",
        retryable=False,
        partial_output=partial_output,
        attempt=attempt,
        next_action="查看错误详情后从最近检查点继续",
        message="Runtime 未能完成本轮响应",
        request_id=request_id,
    )


__all__ = ["ProviderStreamError", "StreamErrorDetails", "classify_stream_error"]
