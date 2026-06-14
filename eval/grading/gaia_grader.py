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


def _try_float(s: str) -> float | None:
    """Parse 's' as float after stripping $/€/£/¥ and commas. Return None if NA."""
    if not s:
        return None
    cleaned = re.sub(r"[$€£¥,]", "", s).strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def question_scorer(model_answer: str, ground_truth: str) -> bool:
    """Return True iff model_answer matches ground_truth.

    Rules (in order):
      1. If both parse as float: equal within 1% relative tolerance.
      2. If ground_truth contains a comma: treat both as multisets of items,
         normalized element-wise (order-insensitive, exact-element match).
      3. Else: normalized string equality.
    """
    if model_answer is None:
        return False
    gt_f = _try_float(ground_truth)
    ma_f = _try_float(model_answer)
    if gt_f is not None and ma_f is not None:
        if gt_f == 0:
            return abs(ma_f) < 1e-9
        return abs(ma_f - gt_f) / abs(gt_f) <= 0.01

    if "," in ground_truth:
        gt_items = {_normalize_str(x) for x in ground_truth.split(",") if x.strip()}
        ma_items = {_normalize_str(x) for x in model_answer.split(",") if x.strip()}
        return gt_items == ma_items

    return _normalize_str(model_answer) == _normalize_str(ground_truth)


_FINAL_ANSWER_RE = re.compile(
    r"final\s+answer\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def extract_final_answer(assistant_content: str) -> str:
    """Prefer 'FINAL ANSWER: X' (case-insensitive); else return last paragraph.

    Returns empty string for empty input.
    """
    if not assistant_content or not assistant_content.strip():
        return ""
    m = _FINAL_ANSWER_RE.search(assistant_content)
    if m:
        return m.group(1).strip()
    # Fallback: last non-empty paragraph
    paragraphs = [p.strip() for p in assistant_content.split("\n\n") if p.strip()]
    return paragraphs[-1] if paragraphs else ""


