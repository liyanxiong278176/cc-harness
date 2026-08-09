from __future__ import annotations

import json
import math

import pytest

from eval.core import canonical_json_bytes, content_fingerprint


def test_canonical_json_is_order_independent_compact_utf8() -> None:
    first = {"z": None, "name": "评估", "nested": {"b": 2, "a": 1}}
    second = {"nested": {"a": 1, "b": 2}, "name": "评估", "z": None}

    expected = '{"name":"评估","nested":{"a":1,"b":2},"z":null}'.encode()
    assert canonical_json_bytes(first) == expected
    assert canonical_json_bytes(second) == expected
    assert content_fingerprint(first) == content_fingerprint(second)


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_json_bytes({"score": math.nan})


def test_fingerprint_matches_canonical_sha256_shape() -> None:
    fingerprint = content_fingerprint({"status": "pass"})
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 64
    int(fingerprint.removeprefix("sha256:"), 16)
    assert json.loads(canonical_json_bytes({"status": "pass"})) == {"status": "pass"}
