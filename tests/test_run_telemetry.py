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
    assert [item["invocation_id"] for item in summary["invocations"]] == [
        "invocation-1",
        "invocation-2",
    ]
    assert summary["invocations"][0]["cache_hit_ratio"] == pytest.approx(0.8)
    assert summary["invocations"][1]["cost_status"] == "reported"


def test_missing_provider_price_is_explicitly_incomplete() -> None:
    summary = aggregate_model_usage(
        [_event("ModelInvocationFinished", {"input_tokens": 10}, status="succeeded")]
    )
    assert summary["cost_observed"] is True
    assert summary["reported_cost"] is None
    assert summary["cost_status"] == "incomplete"
    assert summary["cost_complete"] is False


def test_provider_metadata_stop_reason_and_cache_ratio_are_aggregated() -> None:
    summary = aggregate_model_usage(
        [
            _event(
                "ModelInvocationFinished",
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 75,
                    "output_tokens": 5,
                    "model_calls": 1,
                    "provider": "api.example.test",
                    "model": "model-a",
                    "reported_cost": 0.01,
                    "reported_cost_currency": "USD",
                    "provider_metadata": {
                        "id": "resp-1",
                        "request_id": "req-1",
                        "authorization": "must-not-leak",
                    },
                },
                status="succeeded",
                stop_reason="stop",
            )
        ]
    )
    assert summary["cache_hit_ratio"] == pytest.approx(0.75)
    assert summary["stop_reasons"] == {"stop": 1}
    assert summary["provider_metadata"] == [{"id": "resp-1", "request_id": "req-1"}]


def test_provider_cost_without_currency_remains_a_direct_fact() -> None:
    summary = aggregate_model_usage(
        [_event("ModelInvocationFinished", {"input_tokens": 10, "reported_cost": 0.1}, status="succeeded")]
    )
    assert summary["reported_cost"] == pytest.approx(0.1)
    # An amount without a currency is still a directly reported provider fact;
    # the status is complete, but the caller can see that currency is unknown.
    assert summary["cost_status"] == "reported"
    assert summary["reported_cost_currency"] is None


def test_each_invocation_retains_failure_and_cost_status_without_inference() -> None:
    summary = aggregate_model_usage(
        [
            _event(
                "ModelInvocationFinished",
                {"input_tokens": 10, "output_tokens": 2, "model_calls": 1},
                invocation_id="call-1",
                status="failed",
                error="transport reset",
            ),
            _event(
                "ModelInvocationFinished",
                {
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "model_calls": 1,
                    "reported_cost": 0.02,
                    "reported_cost_currency": "USD",
                },
                invocation_id="call-2",
                status="succeeded",
            ),
        ]
    )
    assert summary["invocations"][0]["invocation_id"] == "call-1"
    assert summary["invocations"][0]["cost_status"] == "unavailable"
    assert summary["invocations"][0]["error"] == "transport reset"
    assert summary["invocations"][1]["reported_cost"] == pytest.approx(0.02)
    assert summary["cost_status"] == "incomplete"


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
