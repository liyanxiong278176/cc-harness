import json

import pytest

from eval.harbor.cc_output import parse_cc_harness_result


def _result(**usage_updates) -> str:
    usage = {
        "input_tokens": 100,
        "uncached_input_tokens": 20,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 80,
        "output_tokens": 10,
        "model_calls": 3,
        "tool_calls": 4,
        **usage_updates,
    }
    return json.dumps(
        {
            "schema_version": "cc-harness.print-result.v1",
            "resolved_model": "deepseek-v4-flash",
            "error": None,
            "usage": usage,
        }
    )


def test_parses_cc_harness_usage_for_harbor() -> None:
    parsed = parse_cc_harness_result(_result())

    assert parsed["input_tokens"] == 100
    assert parsed["cache_read_input_tokens"] == 80
    assert parsed["cost_microusd"] == 390
    assert parsed["pricing_contract_digest"].startswith("sha256:")


def test_rejects_inconsistent_cache_breakdown() -> None:
    with pytest.raises(ValueError, match="does not sum"):
        parse_cc_harness_result(_result(cache_read_input_tokens=79))
