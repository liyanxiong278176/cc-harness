"""Parse cc-harness JSON output into Harbor-compatible usage telemetry."""

from __future__ import annotations

import json
from typing import Any

from eval.launch import PARITY_MODEL, PARITY_PRICING


def parse_cc_harness_result(stdout: str) -> dict[str, Any]:
    try:
        documents = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ValueError(f"cc-harness output is not valid JSONL: {exc}") from exc
    if not documents or not all(isinstance(item, dict) for item in documents):
        raise ValueError("cc-harness output contains no JSON objects")
    result = documents[-1]
    if result.get("schema_version") != "cc-harness.print-result.v1":
        raise ValueError("cc-harness result schema is missing")
    if error := result.get("error"):
        raise ValueError(f"cc-harness reported an error: {error}")
    if result.get("resolved_model") != PARITY_MODEL:
        raise ValueError("cc-harness resolved model does not match the parity contract")
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise TypeError("cc-harness result lacks usage telemetry")
    input_tokens = _nonnegative_int(usage.get("input_tokens"), "input_tokens")
    cache_read = _nonnegative_int(
        usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens"
    )
    cache_creation = _nonnegative_int(
        usage.get("cache_creation_input_tokens", 0),
        "cache_creation_input_tokens",
    )
    uncached = _nonnegative_int(
        usage.get("uncached_input_tokens", input_tokens), "uncached_input_tokens"
    )
    if uncached + cache_creation + cache_read != input_tokens:
        raise ValueError("cc-harness cache token breakdown does not sum to input_tokens")
    output_tokens = _nonnegative_int(usage.get("output_tokens"), "output_tokens")
    cost_microusd = PARITY_PRICING.cost_microusd(
        uncached_input_tokens=uncached,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        output_tokens=output_tokens,
    )
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "model_calls": _nonnegative_int(usage.get("model_calls"), "model_calls"),
        "tool_calls": _nonnegative_int(usage.get("tool_calls"), "tool_calls"),
        "cost_microusd": cost_microusd,
        "pricing_contract_digest": PARITY_PRICING.digest,
        "resolved_model": PARITY_MODEL,
    }


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"cc-harness usage has invalid {field}")
    return value
