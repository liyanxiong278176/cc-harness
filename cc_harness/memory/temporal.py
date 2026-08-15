"""Deterministic temporal metadata and bounded relevance helpers.

LoCoMo historical facts are written as ordinary memory atoms, but temporal QA
needs more than a raw lexical date match.  This module keeps the production
memory path dependency-free: it preserves the original expressions, adds a
small normalized anchor index, and exposes a conservative score used only for
retrieval ordering.  It never changes the benchmark scorer or invents an
event date when a statement has no explicit date.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from .extract import extract_dates

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|"
    r"Sep|Sept|Oct|Nov|Dec)\s+(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_FULL_NAMED_DATE_RE = re.compile(
    r"\b(?:(?P<day>\d{1,2})\s+)?(?P<month>January|February|March|April|May|June|"
    r"July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|"
    r"Jul|Aug|Sep|Sept|Oct|Nov|Dec)(?:\s+(?P<day_after>\d{1,2}))?"
    r"(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>(?:19|20)\d{2}))?\b",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"\b(?P<year>(?:19|20)\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})\b")
_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<first>\d{1,2})[-/](?P<second>\d{1,2})[-/](?P<year>\d{2,4})\b"
)
_RELATION_RE = re.compile(
    r"\b(?P<relation>before|after|earlier|later|around|during|week of|"
    r"the week before|the week after|few days before|few days after)\b",
    re.IGNORECASE,
)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized.casefold() not in seen:
            seen.add(normalized.casefold())
            out.append(normalized)
    return out


def _iso_or_granular(year: int, month: int | None = None, day: int | None = None) -> str:
    if month is None:
        return f"{year:04d}"
    if day is None:
        return f"{year:04d}-{month:02d}"
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return f"{year:04d}-{month:02d}"


def _normalize_expression(expression: str) -> str | None:
    value = expression.strip().replace(",", "")
    iso = _ISO_RE.fullmatch(value)
    if iso:
        return _iso_or_granular(
            int(iso.group("year")), int(iso.group("month")), int(iso.group("day"))
        )
    numeric = _NUMERIC_DATE_RE.fullmatch(value)
    if numeric:
        year = int(numeric.group("year"))
        year += 2000 if year < 100 else 0
        first = int(numeric.group("first"))
        second = int(numeric.group("second"))
        # LoCoMo uses mostly month/day/year, but retain a granular anchor when
        # the order is ambiguous instead of asserting a false exact date.
        if first > 12 and second <= 12:
            return _iso_or_granular(year, second, first)
        if second > 12 and first <= 12:
            return _iso_or_granular(year, first, second)
        return f"{year:04d}-{first:02d}-{second:02d}"
    month_year = _MONTH_YEAR_RE.fullmatch(value)
    if month_year:
        month = _MONTHS.get(month_year.group("month").casefold())
        return _iso_or_granular(int(month_year.group("year")), month) if month else None
    named = _FULL_NAMED_DATE_RE.fullmatch(value)
    if named:
        year_text = named.group("year")
        month_text = named.group("month")
        day_text = named.group("day") or named.group("day_after")
        month = _MONTHS.get(month_text.casefold())
        if month is not None:
            if year_text:
                return _iso_or_granular(int(year_text), month, int(day_text) if day_text else None)
            return None
    if _YEAR_RE.fullmatch(value):
        return value
    return None


def extract_temporal_metadata(text: str, *, session_timestamp: str | None = None) -> dict[str, Any]:
    """Return auditable date expressions, anchors and ordering relations."""

    expressions = _unique(extract_dates(text))
    # ``extract_dates`` deliberately does not classify a year by itself.
    expressions.extend(_unique(_YEAR_RE.findall(text)))
    expressions = _unique(expressions)
    anchors = _unique(
        [normalized for expression in expressions if (normalized := _normalize_expression(expression))]
    )
    relations = _unique([match.group("relation").casefold() for match in _RELATION_RE.finditer(text)])
    session_anchor = None
    if session_timestamp:
        session_metadata = extract_temporal_metadata(session_timestamp)
        session_anchor = (session_metadata.get("anchors") or [None])[0]
    return {
        "expressions": expressions,
        "anchors": anchors,
        "relations": relations,
        "session_anchor": session_anchor,
    }


def temporal_query_metadata(query: str) -> dict[str, Any]:
    """Build metadata for a question without using its reference answer."""

    return extract_temporal_metadata(query)


def _parse_anchor(value: str) -> date | None:
    try:
        if len(value) == 4:
            return date(int(value), 1, 1)
        if len(value) == 7:
            return date.fromisoformat(value + "-01")
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def temporal_relevance(query: str, text: str, provenance_json: str = "{}") -> float:
    """Return a conservative [0, 1] temporal relevance boost.

    The score rewards shared normalized anchors and respects explicit before /
    after language when both sides expose a full date. Missing metadata yields
    zero rather than a guessed ordering.
    """

    question = temporal_query_metadata(query)
    if not question["expressions"] and not question["relations"]:
        return 0.0
    try:
        provenance = json.loads(provenance_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        provenance = {}
    candidate = provenance.get("temporal") if isinstance(provenance, dict) else None
    if not isinstance(candidate, dict):
        candidate = extract_temporal_metadata(text)
    q_anchors = set(question.get("anchors") or [])
    c_anchors = set(candidate.get("anchors") or [])
    overlap = q_anchors & c_anchors
    score = 0.45 if overlap else 0.0
    q_relations = {str(item).casefold() for item in question.get("relations") or []}
    if q_relations and c_anchors and q_anchors:
        q_dates = [_parse_anchor(item) for item in q_anchors]
        c_dates = [_parse_anchor(item) for item in c_anchors]
        q_full = [item for item in q_dates if item and item.day != 1]
        c_full = [item for item in c_dates if item and item.day != 1]
        if q_full and c_full:
            q_anchor = q_full[0]
            c_anchor = c_full[0]
            if (
                ("before" in q_relations or "earlier" in q_relations)
                and c_anchor <= q_anchor
            ) or (
                ("after" in q_relations or "later" in q_relations)
                and c_anchor >= q_anchor
            ):
                score += 0.35
            elif "around" in q_relations and abs((c_anchor - q_anchor).days) <= 31:
                score += 0.25
    if not overlap and candidate.get("session_anchor") in q_anchors:
        score += 0.2
    return min(score, 1.0)
