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
from cc_harness.prompts import SUMMARY_MARKER_KEY


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
    config: ContextConfig | None = None,
    *,
    cfg: ContextConfig | None = None,
) -> CompactionStats:
    """Mutate messages in place: truncate long tool outputs and user code blocks.

    Skip: protect zone, protected tools, assistant content, user prose.
    """
    # Back-compat: callers historically used `cfg=cfg`. Prefer `config`.
    if config is None:
        config = cfg
    snipped = 0
    upper = min(protect_until, len(messages))
    for i in range(0, upper):
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            tool_name = _resolve_tool_name(messages[:i + 1], m.get("tool_call_id"))
            if _is_protected_tool_name(tool_name, config._compiled_patterns):
                continue
            content = m.get("content")
            if isinstance(content, str):
                new_content = _snip_text_lines(content, config.snip_head_lines, config.snip_tail_lines)
                if new_content is not None:
                    m["content"] = new_content
                    snipped += 1
        elif role == "user":
            content = m.get("content")
            if isinstance(content, str):
                new_content = _truncate_user_code_blocks(content, config.snip_head_lines, config.snip_tail_lines)
                if new_content != content:
                    m["content"] = new_content
                    snipped += 1
    return CompactionStats(
        tier=CompactionTier.SNIP,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        messages_snip=snipped,
    )


# --- Tier 2: prune (replace tool output, truncate assistant text) ---

def _prune_assistant_text(content: str) -> str:
    """Reduce content to first sentence + ' [truncated]', or fallback to 200 chars."""
    parts = re.split(SENTENCE_SPLIT_RE, content, maxsplit=1)
    first = parts[0].strip()
    if len(parts) > 1:
        return f"{first} {TIER2_ASSISTANT_TRUNCATION_NOTICE}"
    if len(first) > ASSISTANT_TRUNCATE_FALLBACK_CHARS:
        return f"{first[:ASSISTANT_TRUNCATE_FALLBACK_CHARS]} {TIER2_ASSISTANT_TRUNCATION_NOTICE}"
    return first


def apply_tier2_prune(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
) -> CompactionStats:
    """Mutate messages in place: replace tool outputs with placeholder, truncate assistant text.

    Skip: protect zone, protected tools, summary messages, user prose.
    """
    pruned = 0
    assistant_truncated = 0
    upper = min(protect_until, len(messages))
    for i in range(0, upper):
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            tool_name = _resolve_tool_name(messages[:i + 1], m.get("tool_call_id"))
            if _is_protected_tool_name(tool_name, config._compiled_patterns):
                continue
            if m.get("content") != TIER2_TOOL_PLACEHOLDER:
                m["content"] = TIER2_TOOL_PLACEHOLDER
                pruned += 1
        elif role == "assistant" and not m.get(SUMMARY_MARKER_KEY):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                new_content = _prune_assistant_text(content)
                if new_content != content:
                    m["content"] = new_content
                    assistant_truncated += 1
    return CompactionStats(
        tier=CompactionTier.PRUNE,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        messages_prune=pruned,
        messages_assistant_truncated=assistant_truncated,
    )


# --- Tier 3: summarize (async, LLM-driven) ---

async def _summarize(llm, previous_summary: str, delta_messages: list[dict]) -> str:
    """Call llm.chat with the summary prompt (no tools). Return the merged summary text."""
    from cc_harness.prompts import SUMMARY_SYSTEM_PROMPT, summary_user_prompt
    msgs = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": summary_user_prompt(previous_summary, delta_messages)},
    ]
    content_parts: list[str] = []
    finish_reason: str | None = None
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "content":
            content_parts.append(ev.text)
        elif ev.kind == "done":
            finish_reason = ev.finish_reason
            if ev.content:
                content_parts = [ev.content]
    if not content_parts or not "".join(content_parts).strip():
        raise RuntimeError("LLM returned empty summary")
    if finish_reason not in (None, "stop", "length"):
        # Anything other than normal finish is suspicious but not necessarily fatal
        pass
    return "".join(content_parts)


def _find_previous_summary(messages: list[dict]) -> tuple[int | None, str]:
    """Find the most recent summary-marked assistant message. Return (index, content) or (None, '').

    Summaries are always inserted at index 1 (after system), so the most recent
    summary has the LOWEST index among all summaries. Walk forward and return
    the first match. This preserves the invariant that stats.summary_index is
    recoverable via _find_previous_summary(messages)[0].
    """
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get(SUMMARY_MARKER_KEY):
            return i, m.get("content") or ""
    return None, ""


async def apply_tier3_summarize(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
    counter,
    llm,
) -> CompactionStats:
    """Build delta, call LLM, insert summary message. Returns stats.

    On failure: returns CompactionStats with summarized=False, error=str.
    """
    prev_idx, prev_content = _find_previous_summary(messages)
    if prev_idx is None:
        # Skip system prompt at index 0
        delta_start = 1 if messages and messages[0].get("role") == "system" else 0
    else:
        delta_start = prev_idx + 1
    delta = messages[delta_start:protect_until]

    try:
        new_summary = await _summarize(llm, prev_content, delta)
    except Exception as e:
        return CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
            summarized=False, error=str(e),
        )

    # Insert after system prompt (or at index 0 if no system)
    insert_idx = 1 if messages and messages[0].get("role") == "system" else 0
    summary_msg = {
        "role": "assistant",
        "content": new_summary,
        SUMMARY_MARKER_KEY: True,
    }
    messages.insert(insert_idx, summary_msg)
    return CompactionStats(
        tier=CompactionTier.SUMMARIZE,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        summarized=True, summary_index=insert_idx,
    )


# --- maybe_compact orchestrator ---

async def maybe_compact(
    messages: list[dict],
    tool_specs: list[dict] | None,
    counter,
    config: ContextConfig,
    llm,
) -> CompactionStats:
    """Cascading tier orchestrator. Mutates messages in place.

    Returns CompactionStats; never raises.
    """
    if not config.enabled:
        return CompactionStats(
            tier=CompactionTier.NONE, before_tokens=0, after_tokens=0,
            ratio_before=0.0, ratio_after=0.0,
        )

    before = 0
    after = 0
    ratio = 0.0
    snapshot: list[dict] | None = None
    try:
        before = sum(counter.categorize(messages, tool_specs).values())
        ratio = before / config.context_window
        if ratio < config.tier1_threshold:
            return CompactionStats(
                tier=CompactionTier.NONE, before_tokens=before, after_tokens=before,
                ratio_before=ratio, ratio_after=ratio,
            )
        protect_until = find_protect_boundary(messages, counter, config.protect_zone_tokens)
        if protect_until <= 0 or protect_until >= len(messages):
            return CompactionStats(
                tier=CompactionTier.NONE, before_tokens=before, after_tokens=before,
                ratio_before=ratio, ratio_after=ratio,
            )
        apply_tier1_snip(messages, protect_until, config=config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier2_threshold:
            return CompactionStats(
                tier=CompactionTier.SNIP, before_tokens=before, after_tokens=after,
                ratio_before=ratio, ratio_after=after / config.context_window,
                messages_snip=1,
            )
        apply_tier2_prune(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier3_threshold:
            return CompactionStats(
                tier=CompactionTier.PRUNE, before_tokens=before, after_tokens=after,
                ratio_before=ratio, ratio_after=after / config.context_window,
                messages_prune=1,
            )
        stats = await apply_tier3_summarize(messages, protect_until, config, counter, llm)
        stats.before_tokens = before
        return stats
    except Exception as e:
        if snapshot is None:
            snapshot = [dict(m) for m in messages]  # only in error branch
        return CompactionStats(
            tier=CompactionTier.NONE,
            before_tokens=before, after_tokens=after,
            ratio_before=ratio, ratio_after=ratio,
            error=str(e), before_snapshot=snapshot,
        )
