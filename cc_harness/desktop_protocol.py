"""Versioned local protocol shared by the desktop client and Python sidecar.

The transport is intentionally separate from the message envelope.  The first
desktop client uses the sidecar's stdin/stdout, while the same envelopes can be
carried by a future loopback WebSocket or HTTP/SSE adapter.
"""

from __future__ import annotations

from typing import Any, Mapping


DESKTOP_PROTOCOL_VERSION = 1


class DesktopProtocolError(ValueError):
    """Raised when a desktop bridge message violates the v1 contract."""


def _require_object(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DesktopProtocolError(f"{name} must be a JSON object")
    return value


def validate_request(message: Any) -> dict[str, Any]:
    """Validate and normalize one client-to-sidecar request."""

    raw = _require_object(message, name="request")
    version = raw.get("protocol_version", DESKTOP_PROTOCOL_VERSION)
    if version != DESKTOP_PROTOCOL_VERSION:
        raise DesktopProtocolError(
            f"unsupported desktop protocol version: {version!r}"
        )
    message_type = raw.get("type")
    if not isinstance(message_type, str) or not message_type.strip():
        raise DesktopProtocolError("request type must be a non-empty string")
    request_id = raw.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or len(request_id) > 200
    ):
        raise DesktopProtocolError("request_id must be a short string")
    return {
        "protocol_version": DESKTOP_PROTOCOL_VERSION,
        "request_id": request_id,
        "type": message_type,
        "payload": dict(_require_object(raw.get("payload", {}), name="payload")),
    }


def response(
    request_id: str | None,
    *,
    ok: bool,
    data: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a response envelope without leaking unstructured exceptions."""

    message: dict[str, Any] = {
        "protocol_version": DESKTOP_PROTOCOL_VERSION,
        "message_type": "response",
        "request_id": request_id,
        "ok": ok,
        "data": dict(data or {}),
    }
    if error is not None:
        message["error"] = dict(error)
    return message


def event(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    watch_id: str | None = None,
) -> dict[str, Any]:
    """Build an ordered durable-event envelope for a connected client."""

    return {
        "protocol_version": DESKTOP_PROTOCOL_VERSION,
        "message_type": "event",
        "watch_id": watch_id,
        "run_id": run_id,
        "sequence": sequence,
        "event_type": event_type,
        "payload": dict(payload),
    }


__all__ = [
    "DESKTOP_PROTOCOL_VERSION",
    "DesktopProtocolError",
    "event",
    "response",
    "validate_request",
]
