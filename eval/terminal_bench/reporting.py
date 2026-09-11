"""Infrastructure evidence reporting helpers."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


def summarize_infrastructure_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate guard/recovery events without changing official scores."""

    rows = [dict(event) for event in events]
    categories = Counter(str(row.get("category") or row.get("guard") or "unknown") for row in rows)
    severities = Counter(str(row.get("severity") or "unknown") for row in rows)
    return {"event_count": len(rows), "by_category": dict(categories), "by_severity": dict(severities), "events": rows}


__all__ = ["summarize_infrastructure_events"]
