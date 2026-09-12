"""Pure aggregation helpers for durable model-invocation telemetry.

The event stream is authoritative for a Durable Run.  In particular, a
``ModelInvocationFinished`` event is emitted even when a provider fails before
an assistant message is committed.  Reports and the TUI must therefore prefer
those terminal invocation facts and only use legacy assistant usage payloads
when no invocation facts exist.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Mapping


_USAGE_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _payload(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "payload", None)
    return value if isinstance(value, Mapping) else {}


def _usage(event: Any) -> Mapping[str, Any]:
    value = _payload(event).get("usage")
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _reported_cost(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _provider_identity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_SAFE_METADATA_KEYS = {
    "id",
    "model",
    "object",
    "created",
    "system_fingerprint",
    "service_tier",
    "request_id",
    "response_id",
}


def _bounded_provider_metadata(value: Any) -> dict[str, Any]:
    """Keep only bounded, non-secret provider response identifiers."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _SAFE_METADATA_KEYS:
        raw = value.get(key)
        if isinstance(raw, (str, int, float, bool)):
            result[key] = str(raw)[:512] if isinstance(raw, str) else raw
    return result


def aggregate_model_usage(events: Iterable[Any]) -> dict[str, Any]:
    """Aggregate provider-reported usage from a durable event sequence.

    The returned dictionary intentionally contains no inferred tariff.  A
    missing cost on any observed invocation makes ``cost_status``
    ``"incomplete"`` and leaves ``reported_cost`` as ``None``.
    """

    all_events = tuple(events)
    invocation_events = tuple(
        event for event in all_events
        if getattr(event, "event_type", None) == "ModelInvocationFinished"
    )
    source_events = invocation_events or tuple(
        event for event in all_events
        if getattr(event, "event_type", None) == "AssistantMessageCommitted"
    )
    result: dict[str, Any] = {
        **{field: 0 for field in _USAGE_FIELDS},
        "model_calls": 0,
        "invocation_count": len(source_events),
        "cost_source": "provider",
        "cost_status": "unavailable",
        "reported_cost": None,
        "reported_cost_currency": None,
        "cost_observed": False,
        "cost_complete": False,
        "providers": [],
        "models": [],
        "statuses": {},
        "invocation_statuses": {},
        "stop_reasons": {},
        "provider_metadata": [],
        "invocations": [],
        "provider": None,
        "model": None,
        "cache_hit_ratio": None,
        "context_categories": None,
    }
    if not source_events:
        return result

    costs: list[float | None] = []
    currencies: list[str | None] = []
    providers: set[str] = set()
    models: set[str] = set()
    statuses: Counter[str] = Counter()
    stop_reasons: Counter[str] = Counter()
    provider_metadata: list[dict[str, Any]] = []
    for index, event in enumerate(source_events, 1):
        payload = _payload(event)
        usage = _usage(event)
        for field in _USAGE_FIELDS:
            result[field] += _nonnegative_int(usage.get(field))
        # Invocation events count failed calls as well.  A legacy assistant
        # event has no explicit count, so each committed message is one call.
        result["model_calls"] += max(1, _nonnegative_int(usage.get("model_calls")))
        status = _provider_identity(payload.get("status"))
        if status is not None:
            statuses[status] += 1
        provider = _provider_identity(usage.get("provider"))
        model = _provider_identity(usage.get("model"))
        if provider is not None:
            providers.add(provider)
        if model is not None:
            models.add(model)
        stop_reason = _provider_identity(
            payload.get("stop_reason") or usage.get("stop_reason") or payload.get("finish_reason")
        )
        if stop_reason is not None:
            stop_reasons[stop_reason] += 1
        metadata = _bounded_provider_metadata(
            payload.get("provider_metadata") or usage.get("provider_metadata")
        )
        if metadata and metadata not in provider_metadata and len(provider_metadata) < 32:
            provider_metadata.append(metadata)
        costs.append(_reported_cost(usage.get("reported_cost")))
        raw_currency = _provider_identity(usage.get("reported_cost_currency"))
        currencies.append(raw_currency.upper() if raw_currency is not None else None)

        # Use the current event's usage directly for the per-call envelope;
        # the aggregate counters above intentionally remain cumulative.
        call_input = _nonnegative_int(usage.get("input_tokens"))
        call_cache_read = _nonnegative_int(usage.get("cache_read_input_tokens"))
        call_cache_create = _nonnegative_int(usage.get("cache_creation_input_tokens"))
        call_cost = _reported_cost(usage.get("reported_cost"))
        call_currency = _provider_identity(usage.get("reported_cost_currency"))
        call_status = "reported" if call_cost is not None else "unavailable"
        invocation_id = _provider_identity(
            payload.get("invocation_id") or payload.get("request_id")
        ) or f"invocation-{index}"
        invocation = {
            "invocation_id": invocation_id,
            "status": status or "unknown",
            "provider": provider,
            "model": model,
            "input_tokens": call_input,
            "uncached_input_tokens": _nonnegative_int(usage.get("uncached_input_tokens")),
            "cache_creation_input_tokens": call_cache_create,
            "cache_read_input_tokens": call_cache_read,
            "output_tokens": _nonnegative_int(usage.get("output_tokens")),
            "cache_hit_ratio": (
                call_cache_read / call_input if call_input > 0 else None
            ),
            "reported_cost": call_cost,
            "reported_cost_currency": call_currency.upper() if call_currency else None,
            "cost_status": call_status,
            "stop_reason": stop_reason,
            "duration_ms": payload.get("duration_ms"),
            "provider_metadata": metadata,
        }
        categories = usage.get("context_categories")
        if isinstance(categories, Mapping):
            # Counts are explanatory local telemetry, not billable usage. Keep
            # only non-negative integers and a fixed set of known buckets so
            # untrusted provider payloads cannot inflate the UI or leak data.
            safe_categories = {}
            for name in (
                "user_input",
                "tool_calls",
                "llm_output",
                "system_prompt",
                "summary",
                "tool_definitions",
            ):
                try:
                    safe_categories[name] = max(0, int(categories.get(name, 0) or 0))
                except (TypeError, ValueError):
                    safe_categories[name] = 0
            invocation["context_categories"] = safe_categories
            result["context_categories"] = safe_categories
        if "error" in payload:
            invocation["error"] = str(payload.get("error") or "")[:1_000]
        # Keep the report bounded even if a very long run contains thousands
        # of calls.  Aggregate counters remain complete; this list is the
        # auditable per-call detail used by the TUI and JSON export.
        if len(result["invocations"]) < 512:
            result["invocations"].append(invocation)

    result["providers"] = sorted(providers)
    result["models"] = sorted(models)
    result["provider"] = result["providers"][0] if result["providers"] else None
    result["model"] = result["models"][0] if result["models"] else None
    result["statuses"] = dict(sorted(statuses.items()))
    result["invocation_statuses"] = result["statuses"]
    result["stop_reasons"] = dict(sorted(stop_reasons.items()))
    result["provider_metadata"] = provider_metadata
    total_input = result["input_tokens"]
    result["cache_hit_ratio"] = (
        result["cache_read_input_tokens"] / total_input
        if total_input > 0
        else None
    )
    result["cost_observed"] = True
    non_null_costs = [cost for cost in costs if cost is not None]
    normalized_currencies = set(currencies)
    if (
        non_null_costs
        and len(non_null_costs) == len(costs)
        and len(normalized_currencies) <= 1
    ):
        result["reported_cost"] = sum(non_null_costs)
        result["reported_cost_currency"] = next(iter(normalized_currencies), None)
        result["cost_status"] = "reported"
        result["cost_complete"] = True
    else:
        result["cost_status"] = "incomplete"
    return result


__all__ = ["aggregate_model_usage"]
