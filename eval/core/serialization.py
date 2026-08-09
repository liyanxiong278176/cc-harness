"""Canonical serialization and content identities for evaluation evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible evidence to deterministic UTF-8 bytes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return serialized.encode("utf-8")


def content_fingerprint(value: Any) -> str:
    """Return the canonical SHA-256 content identity for evidence."""

    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"sha256:{digest}"
