"""4-tier waterline context compression for cc-harness.

Public API:
- `find_protect_boundary(messages, counter, budget) -> int`
- `apply_tier1_snip(messages, protect_until, config) -> CompactionStats`
- `apply_tier2_prune(messages, protect_until, config) -> CompactionStats`
- `apply_tier3_summarize(messages, protect_until, config, llm) -> CompactionStats`
- `maybe_compact(messages, tool_specs, counter, config, llm) -> CompactionStats`
- `CompactionTier` (IntEnum), `CompactionStats` (dataclass)
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


# --- Public types ---

class CompactionTier(IntEnum):
    NONE = 0
    SNIP = 1
    PRUNE = 2
    SUMMARIZE = 3


@dataclass
class CompactionStats:
    tier: CompactionTier
    before_tokens: int
    after_tokens: int
    ratio_before: float
    ratio_after: float
    messages_snip: int = 0
    messages_prune: int = 0
    messages_assistant_truncated: int = 0
    summarized: bool = False
    summary_index: int | None = None
    error: str | None = None
    before_snapshot: list[dict] | None = None


# --- Module constants ---

TIER2_TOOL_PLACEHOLDER = "[Old tool result content cleared]"
TIER2_ASSISTANT_TRUNCATION_NOTICE = "[truncated]"
TIER1_CODE_BLOCK_TRUNCATION_NOTICE = "... ({} lines omitted) ..."
SUMMARY_MARKER_KEY = "_compaction_summary"
SENTENCE_SPLIT_RE = r"(?<=[.。!?！？\n])\s*"
ASSISTANT_TRUNCATE_FALLBACK_CHARS = 200


# --- Helpers ---

def _last_user_idx(messages: list[dict]) -> int:
    """Return the index of the last user-role message, or 0 if none."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return 0


def _message_token_count(message: dict, counter) -> int:
    """Estimate token count of a single message (content + tool_calls JSON + tool_call_id)."""
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += counter.count_text(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                total += counter.count_text(item.get("text", ""))
    for tc in (message.get("tool_calls") or []):
        import json as _json
        total += counter.count_text(_json.dumps(tc, ensure_ascii=False))
    tcid = message.get("tool_call_id")
    if tcid:
        total += counter.count_text(tcid)
    return total


# --- find_protect_boundary ---

def find_protect_boundary(messages: list[dict], counter, budget_tokens: int) -> int:
    """Return the smallest index i such that messages[i:] totals at most budget_tokens.

    The boundary ALWAYS clamps at the last user-role message index (or 0 if none),
    so that the most recent user input is always in the protect zone.
    """
    if not messages:
        return 0
    floor = _last_user_idx(messages)
    used = 0
    for i in range(len(messages) - 1, -1, -1):
        used += _message_token_count(messages[i], counter)
        if used > budget_tokens:
            if i <= floor:
                # The last user message alone is bigger than the budget.
                # It is unfillable — clamp at the floor (last user).
                return floor
            return i + 1
    return 0  # entire messages fits in budget; nothing to compress
