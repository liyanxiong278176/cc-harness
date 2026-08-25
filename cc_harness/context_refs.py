"""Opaque, scoped references into authoritative context message streams."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping, Sequence


def message_digest(message: Mapping[str, Any]) -> str:
    encoded = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def messages_digest(messages: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _encode(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> dict[str, Any] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def context_message_ref(context_id: str, index: int, digest: str) -> str:
    return "ctxmsg:" + _encode({"context_id": context_id, "index": int(index), "digest": digest})


def context_scope_ref(
    context_id: str,
    start: int,
    end: int,
    digest: str,
    *,
    version: int | None = None,
) -> str:
    payload: dict[str, Any] = {
        "context_id": context_id,
        "start": int(start),
        "end": int(end),
        "digest": digest,
    }
    if version is not None:
        payload["version"] = int(version)
    return "ctxscope:" + _encode(payload)


def parse_context_ref(value: object) -> tuple[str, dict[str, Any]] | None:
    text = str(value or "")
    if text.startswith("ctxmsg:"):
        payload = _decode(text.removeprefix("ctxmsg:"))
        return ("message", payload) if payload is not None else None
    if text.startswith("ctxscope:"):
        payload = _decode(text.removeprefix("ctxscope:"))
        return ("scope", payload) if payload is not None else None
    return None


__all__ = [
    "context_message_ref",
    "context_scope_ref",
    "message_digest",
    "messages_digest",
    "parse_context_ref",
]
