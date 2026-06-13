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
import re
from dataclasses import dataclass
from enum import IntEnum

from cc_harness.config import ContextConfig


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


# --- Tier 1: snip (zero-cost text truncation) ---

# Code fence: opening ``` (with optional language tag) + body + closing ```
FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)\n```", re.DOTALL)


def _snip_text_lines(content: str, head: int, tail: int) -> str | None:
    """Truncate content to head + tail lines with an omitted marker.

    Returns None if content is too short to bother truncating.
    """
    lines = content.splitlines()
    if len(lines) <= head + tail + 1:
        return None
    kept_head = lines[:head]
    kept_tail = lines[-tail:] if tail > 0 else []
    omitted = len(lines) - head - tail
    marker = TIER1_CODE_BLOCK_TRUNCATION_NOTICE.format(omitted)
    return "\n".join(kept_head + [marker] + kept_tail)


def _truncate_user_code_blocks(content: str, head: int, tail: int) -> str:
    """Apply _snip_text_lines to each ``` fenced code block in content."""
    def _repl(m: re.Match) -> str:
        lang = m.group(1) or ""
        body = m.group(2)
        snipped = _snip_text_lines(body, head, tail)
        if snipped is None:
            return m.group(0)
        return f"```{lang}\n{snipped}\n```"
    return FENCE_RE.sub(_repl, content)


def _is_protected_tool_name(tool_name: str | None, compiled: list[re.Pattern]) -> bool:
    if not tool_name:
        return False
    return any(p.search(tool_name) for p in compiled)


def _resolve_tool_name(messages: list[dict], tool_call_id: str | None) -> str | None:
    """Walk backwards from given tool message to find the assistant's tool_calls and return the function name."""
    if not tool_call_id:
        return None
    for j in range(len(messages) - 1, -1, -1):
        m = messages[j]
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("id") == tool_call_id:
                    fn = tc.get("function", {})
                    return fn.get("name")
    return None


def apply_tier1_snip(
    messages: list[dict],
    protect_until: int,
    cfg: ContextConfig,
) -> CompactionStats:
    """Mutate messages in place: truncate long tool outputs and user code blocks.

    Skip: protect zone, protected tools, assistant content, user prose.
    """
    snipped = 0
    upper = min(protect_until, len(messages))
    for i in range(0, upper):
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            tool_name = _resolve_tool_name(messages[:i + 1], m.get("tool_call_id"))
            if _is_protected_tool_name(tool_name, cfg._compiled_patterns):
                continue
            content = m.get("content")
            if isinstance(content, str):
                new_content = _snip_text_lines(content, cfg.snip_head_lines, cfg.snip_tail_lines)
                if new_content is not None:
                    m["content"] = new_content
                    snipped += 1
        elif role == "user":
            content = m.get("content")
            if isinstance(content, str):
                new_content = _truncate_user_code_blocks(content, cfg.snip_head_lines, cfg.snip_tail_lines)
                if new_content != content:
                    m["content"] = new_content
                    snipped += 1
    return CompactionStats(
        tier=CompactionTier.SNIP,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        messages_snip=snipped,
    )
