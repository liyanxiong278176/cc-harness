"""GAIA scoring — ported from the official evaluation harness.

Reference: gaia-benchmark/leaderboard scoring code.
"""
from __future__ import annotations
import re
import string


_ARTICLE_RE = re.compile(r"\b(a|an|the)\b", re.IGNORECASE)
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_WS_RE = re.compile(r"\s+")


def _normalize_str(s: str) -> str:
    """Lower, strip articles + punctuation, collapse whitespace."""
    if not s:
        return ""
    s = s.lower()
    s = _ARTICLE_RE.sub(" ", s)
    s = s.translate(_PUNCT_TABLE)
    s = _WS_RE.sub(" ", s).strip()
    return s
