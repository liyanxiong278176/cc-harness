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
    }
    if not source_events:
        return result

    costs: list[float | None] = []
    currencies: list[str | None] = []
    providers: set[str] = set()
    models: set[str] = set()
    statuses: Counter[str] = Counter()
    for event in source_events:
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
        costs.append(_reported_cost(usage.get("reported_cost")))
        raw_currency = _provider_identity(usage.get("reported_cost_currency"))
        currencies.append(raw_currency.upper() if raw_currency is not None else None)

    result["providers"] = sorted(providers)
    result["models"] = sorted(models)
    result["statuses"] = dict(sorted(statuses.items()))
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
