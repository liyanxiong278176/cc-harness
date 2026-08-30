from types import SimpleNamespace

import pytest

from cc_harness.run_telemetry import aggregate_model_usage
from cc_harness.repl import _format_durable_usage


def _event(event_type: str, usage: dict, **payload):
    return SimpleNamespace(event_type=event_type, payload={"usage": usage, **payload})


def test_invocation_facts_include_failed_calls_and_ignore_legacy_duplicates() -> None:
    summary = aggregate_model_usage(
        [
            _event(
                "ModelInvocationFinished",
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 80,
                    "output_tokens": 4,
                    "model_calls": 1,
                    "reported_cost": 0.1,
                    "reported_cost_currency": "usd",
                    "provider": "api.example.test",
                    "model": "model-a",
                },
                status="succeeded",
            ),
            _event(
                "ModelInvocationFinished",
                {
                    "input_tokens": 50,
                    "output_tokens": 2,
                    "model_calls": 1,
                    "reported_cost": 0.2,
                    "reported_cost_currency": "USD",
                    "provider": "api.example.test",
                    "model": "model-a",
                },
                status="failed",
            ),
            _event(
                "AssistantMessageCommitted",
                {"input_tokens": 999, "model_calls": 99, "reported_cost": 9},
            ),
        ]
    )
    assert summary["input_tokens"] == 150
    assert summary["output_tokens"] == 6
    assert summary["model_calls"] == 2
    assert summary["reported_cost"] == pytest.approx(0.3)
    assert summary["cost_status"] == "reported"
    assert summary["statuses"] == {"failed": 1, "succeeded": 1}
    assert summary["models"] == ["model-a"]


def test_missing_provider_price_is_explicitly_incomplete() -> None:
    summary = aggregate_model_usage(
        [_event("ModelInvocationFinished", {"input_tokens": 10}, status="succeeded")]
    )
    assert summary["cost_observed"] is True
    assert summary["reported_cost"] is None
    assert summary["cost_status"] == "incomplete"
    assert summary["cost_complete"] is False


def test_durable_usage_display_is_compact_and_has_no_prompt_text() -> None:
    text = _format_durable_usage(
        {
            "input_tokens": 1_200,
            "output_tokens": 30,
            "cache_read_input_tokens": 900,
            "model_calls": 2,
            "cost_status": "reported",
            "reported_cost": 0.42,
            "reported_cost_currency": "USD",
            "models": ["model-a"],
        }
    )
    assert "input=1,200" in text
    assert "cache_hit=900/1,200 (75%)" in text
    assert "cost=USD 0.42" in text
    assert "prompt" not in text.casefold()
