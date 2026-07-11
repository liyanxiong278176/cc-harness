"""4-tier context compaction for cc-harness (Plan3).

``maybe_compact`` is invoked before each LLM call in the ReAct loop. It walks a
token-budget cascade:

- Tier 1 Snip  (ratio >= tier1): truncate long tool outputs / user code blocks (head/tail).
- Tier 2 Prune (ratio >= tier2): tool content -> placeholder; assistant text -> first sentence.
- Tier 3 Summarize (ratio >= tier3): LLM incremental summary (prev + delta -> new summary).

A **protect zone** (the most recent ~``protect_zone_tokens`` plus the last user
message) is never touched. All tiers mutate ``messages`` in place. Failures are
isolated: ``maybe_compact`` never raises — it returns ``CompactionStats(error=...)``.

Design spec: ``docs/superpowers/specs/2026-06-12-context-compaction-design.md``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from cc_harness.config import ContextConfig
from cc_harness.prompts import (
    SUMMARY_SYSTEM_PROMPT,
    _render_messages_for_summary,
    summary_user_prompt,
)
from cc_harness.tokens import SUMMARY_MARKER_KEY, TokenCounter

# Public constants -----------------------------------------------------------

TIER2_TOOL_PLACEHOLDER = "[Old tool result content cleared]"
TRUNCATED_MARKER = " [truncated]"
OMITTED_TEMPLATE = "... ({n} lines omitted) ..."

# Tier 2 assistant fallback when no sentence boundary is present.
_FALLBACK_CHARS = 200

# 3-group fence regex for user ```` ``` ```` code blocks (no nested-fence support).
_CODE_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)\n```", re.DOTALL)

# Sentence boundary: lookbehind after CJK/Latin punctuation or newline.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.。!?！？\n])\s*")


# Data model -----------------------------------------------------------------

class CompactionTier(IntEnum):
    """Compaction tier reached by ``maybe_compact`` (higher = more aggressive)."""

    NONE = 0
    SNIP = 1
    PRUNE = 2
    SUMMARIZE = 3


@dataclass
class CompactionStats:
    """Outcome of one ``maybe_compact`` invocation.

    ``before_snapshot`` is populated only on the exception path (deep copy is
    otherwise avoided to keep the hot path zero-cost).
    """

    tier: CompactionTier
    before_tokens: int
    after_tokens: int
    ratio_before: float
    ratio_after: float
    messages_snip: int = 0                    # tool outputs snipped (Tier 1)
    messages_prune: int = 0                   # tool outputs pruned (Tier 2)
    messages_assistant_truncated: int = 0     # assistant texts truncated (Tier 2)
    summarized: bool = False                  # Tier 3 produced a new summary
    summary_index: int | None = None          # insert index of the new summary
    error: str | None = None                  # exception message (if any)
    before_snapshot: list[dict] | None = None  # debug snapshot (exception path only)


# Helpers --------------------------------------------------------------------


def _count_msg_tokens(message: dict, counter: TokenCounter) -> int:
    """Approximate token count of a single message (content + tool_calls json)."""
    content = message.get("content")
    total = counter.count_text(content if isinstance(content, str) else "")
    for tc in (message.get("tool_calls") or []):
        total += counter.count_text(json.dumps(tc, ensure_ascii=False))
    return total


def _last_user_idx(messages: list[dict]) -> int | None:
    """Index of the last ``role == user`` message, or None if absent."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return None


def _compile_protected_patterns(config: ContextConfig) -> list[re.Pattern]:
    """Compile ``protected_tool_patterns`` regex strings (empty list is OK)."""
    compiled: list[re.Pattern] = []
    for pat in config.protected_tool_patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error:
            # Skip un-compilable patterns rather than crashing compaction.
            continue
    return compiled


def _is_protected_tool(message: dict, compiled: list[re.Pattern]) -> bool:
    """True if the message is a ``role == tool`` whose name matches any pattern."""
    if message.get("role") != "tool":
        return False
    name = message.get("name") or ""
    return any(p.search(name) for p in compiled)


def _snip_lines(text: str, head: int, tail: int) -> str | None:
    """Snip multi-line ``text`` to ``head`` first + omission marker + ``tail`` last.

    Returns ``None`` when the content is too short to snip (``len(lines) <=
    head + tail + 1``), so the caller can treat it as a no-op. A leading newline
    is stripped first (tool outputs sometimes begin with one).
    """
    stripped = text.lstrip("\n")
    lines = stripped.splitlines()
    if len(lines) <= head + tail + 1:
        return None
    skipped = len(lines) - head - tail
    out = lines[:head] + [OMITTED_TEMPLATE.format(n=skipped)]
    if tail > 0:
        out += lines[-tail:]
    return "\n".join(out)


def _snip_code_body(body: str, head: int, tail: int, *, force: bool = False) -> str:
    """Snip the body of a ```` ``` ```` code block.

    When ``force`` is False (Tier 1), content shorter than ``head + tail + 1``
    lines is returned unchanged. When ``force`` is True (Tier 2), the threshold
    check is skipped — any block with more lines than ``head + tail`` is cut.
    Returns the (possibly snipped) body string.
    """
    lines = body.splitlines()
    threshold = (head + tail) if force else (head + tail + 1)
    if len(lines) <= threshold:
        return body
    skipped = len(lines) - head - tail
    out = lines[:head] + [OMITTED_TEMPLATE.format(n=skipped)]
    if tail > 0:
        out += lines[-tail:]
    return "\n".join(out)


# Protect boundary -----------------------------------------------------------


def find_protect_boundary(
    messages: list[dict], counter: TokenCounter, budget_tokens: int
) -> int:
    """Return the slice index ``b`` such that ``messages[b:]`` is the protect zone.

    Walks from the tail accumulating tokens; once the running total reaches
    ``budget_tokens``, returns ``position + 1``. Clamp: the boundary never
    crosses the last ``role == user`` message (so the most recent user input is
    always protected). Returns 0 when the whole list fits the budget (all
    protected).
    """
    if not messages:
        return 0
    cumulative = 0
    boundary = 0
    for i in range(len(messages) - 1, -1, -1):
        if cumulative >= budget_tokens:
            boundary = i + 1
            break
        cumulative += _count_msg_tokens(messages[i], counter)
    # Clamp: never move the boundary past the last user message index.
    last_user = _last_user_idx(messages)
    if last_user is not None and boundary > last_user:
        boundary = last_user
    return boundary


# Tier 1: Snip ---------------------------------------------------------------


def apply_tier1_snip(
    messages: list[dict], protect_until: int, config: ContextConfig
) -> int:
    """Truncate long tool outputs and user code blocks (string-level, zero LLM cost).

    Mutates ``messages[:protect_until]`` in place. Skips: the protect zone,
    ``role == assistant`` content, protected-tool-pattern matches, and content
    shorter than ``head + tail + 1`` lines. Returns the count of modified
    messages (tool outputs + user code blocks).
    """
    compiled = _compile_protected_patterns(config)
    head, tail = config.snip_head_lines, config.snip_tail_lines
    snipped = 0

    for i in range(min(protect_until, len(messages))):
        m = messages[i]
        role = m.get("role")

        if role == "tool":
            if _is_protected_tool(m, compiled):
                continue
            content = m.get("content")
            if not isinstance(content, str):
                continue
            new = _snip_lines(content, head, tail)
            if new is not None:
                m["content"] = new
                snipped += 1

        elif role == "user":
            content = m.get("content")
            if not isinstance(content, str):
                continue
            new_content = _CODE_FENCE_RE.sub(
                lambda match: _rebuild_fence(match, head, tail, force=False),
                content,
            )
            if new_content != content:
                m["content"] = new_content
                snipped += 1
    return snipped


def _rebuild_fence(match: re.Match, head: int, tail: int, *, force: bool) -> str:
    """Rebuild a ```` ``` ```` fence with a (possibly) snipped body."""
    lang = match.group(1)
    body = match.group(2)
    new_body = _snip_code_body(body, head, tail, force=force)
    if new_body == body:
        return match.group(0)
    return f"```{lang}\n{new_body}\n```"


# Tier 2: Prune --------------------------------------------------------------


def apply_tier2_prune(
    messages: list[dict], protect_until: int, config: ContextConfig
) -> tuple[int, int]:
    """Replace tool outputs with a placeholder and truncate assistant text.

    Mutates ``messages[:protect_until]`` in place. Tool messages keep their
    slot (only ``content`` changes) to preserve the ``tool_use``/``tool_result``
    pairing. Assistant messages keep ``tool_calls`` and are never deleted.
    Summary-marked assistants, protected tools, and the protect zone are
    skipped. Returns ``(tool_pruned, assistant_truncated)`` counts.
    """
    compiled = _compile_protected_patterns(config)
    head, tail = 1, 0  # Tier 2 user-code-block aggressiveness (spec 「Tier 2」).
    pruned_tool = 0
    truncated_asst = 0

    for i in range(min(protect_until, len(messages))):
        m = messages[i]
        role = m.get("role")

        if role == "tool":
            if _is_protected_tool(m, compiled):
                continue
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = TIER2_TOOL_PLACEHOLDER
                pruned_tool += 1

        elif role == "assistant":
            # Never touch a Tier-3 summary message (self-destruction guard).
            if m.get(SUMMARY_MARKER_KEY):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content:
                continue
            new = _truncate_assistant(content)
            if new is not None:
                m["content"] = new
                truncated_asst += 1

        elif role == "user":
            content = m.get("content")
            if not isinstance(content, str):
                continue
            new_content = _CODE_FENCE_RE.sub(
                lambda match: _rebuild_fence(match, head, tail, force=True),
                content,
            )
            if new_content != content:
                m["content"] = new_content

    return pruned_tool, truncated_asst


def _truncate_assistant(content: str) -> str | None:
    """Reduce an assistant text to its first sentence (+ truncation marker).

    Returns ``None`` when nothing was actually shortened (no boundary and the
    content is already under the fallback length). Falls back to the first
    ``_FALLBACK_CHARS`` characters when no sentence punctuation is found.
    """
    parts = _SENTENCE_SPLIT_RE.split(content, maxsplit=1)
    if len(parts) > 1:
        return parts[0] + TRUNCATED_MARKER
    if len(content) > _FALLBACK_CHARS:
        return content[:_FALLBACK_CHARS] + TRUNCATED_MARKER
    return None


# Delta size cap (spec 2026-06-12 「Delta 大小上限」 L236-237) ----------------

# Marker prefixed when a Tier-3 delta is truncated to fit the summary budget.
_DELTA_TRUNCATED_TEMPLATE = "... (delta truncated, {n} earlier messages omitted) ..."


def _cap_delta_size(
    rendered_delta: str,
    delta_messages: list[dict],
    config: ContextConfig,
    counter: TokenCounter | None,
) -> str:
    """Enforce the Tier-3 delta size cap (spec L236-237).

    If the serialized delta exceeds ``summarize_max_output_tokens * 4`` tokens
    (default ≈ 8K), truncate to 70% of the budget — keeping the most recent
    messages and dropping earlier ones — and prefix a truncation marker naming
    how many earlier messages were omitted. Prevents the Tier-3 summary LLM call
    from itself overflowing the context window or being silently truncated by
    the provider. When under the cap, ``rendered_delta`` is returned unchanged.
    """
    token_counter = counter if counter is not None else TokenCounter()
    budget = config.summarize_max_output_tokens
    cap = budget * 4
    if token_counter.count_text(rendered_delta) <= cap:
        return rendered_delta

    # Over the cap: keep the most recent messages that fit in 70% of the budget.
    keep_budget = int(budget * 0.7)
    kept: list[dict] = []
    running = 0
    for m in reversed(delta_messages):
        t = token_counter.count_text(_render_messages_for_summary([m]))
        # Always keep at least the most recent message, even if it alone exceeds
        # the budget (an empty delta would starve the summarizer).
        if kept and running + t > keep_budget:
            break
        kept.append(m)
        running += t
    kept.reverse()
    omitted = len(delta_messages) - len(kept)
    body = _render_messages_for_summary(kept)
    return f"{_DELTA_TRUNCATED_TEMPLATE.format(n=omitted)}\n\n{body}"


# Tier 3: Summarize ----------------------------------------------------------


def _find_previous_summary(messages: list[dict]) -> tuple[int, str] | None:
    """Reverse-scan for the most recent compaction summary message.

    Returns ``(index, content)`` for the last ``role == assistant`` message
    carrying the ``_compaction_summary`` marker, or ``None`` if none exists.
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and m.get(SUMMARY_MARKER_KEY):
            content = m.get("content")
            if isinstance(content, str):
                return (i, content)
    return None


async def apply_tier3_summarize(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
    llm: Any,
    counter: TokenCounter | None = None,
) -> CompactionStats:
    """Tier 3: LLM-powered incremental summarization.

    Finds the previous summary (if any), computes the delta (messages between
    prev summary and protect zone), asks the LLM to merge prev + delta into a
    new summary, then inserts it at index 1 (after system) or 0. The old
    summary (if found) is replaced. ``tools=None`` is passed to ``llm.chat``
    (spec: summary LLM must not call tools).

    The serialized delta is capped at ``summarize_max_output_tokens * 4`` tokens
    (spec L236-237): over the cap it is truncated to 70% of the budget with a
    truncation marker, so the summary LLM call cannot itself overflow the
    context window or be silently truncated by the provider.

    Errors are caught and surfaced via ``CompactionStats.error`` — this
    function never raises (Tier 3 failure must not kill the cascade).
    """
    try:
        # 1. Find previous summary
        prev = _find_previous_summary(messages)
        if prev is not None:
            prev_idx, prev_content = prev
            delta_start = prev_idx + 1
        else:
            prev_content = None
            delta_start = 1 if (messages and messages[0].get("role") == "system") else 0

        # 2. Slice delta (messages between prev/system and protect zone)
        delta_messages = messages[delta_start:max(protect_until, delta_start)]

        # 3. Build summary prompt (cap delta size — spec L236-237)
        rendered_delta = _render_messages_for_summary(delta_messages)
        rendered_delta = _cap_delta_size(
            rendered_delta, delta_messages, config, counter
        )
        user_prompt = summary_user_prompt(prev_content, rendered_delta)
        summary_messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        # 4. Call LLM (tools=None — spec mandates no tools)
        content = ""
        async for ev in llm.chat(summary_messages, tools=None):
            if ev.kind == "done":
                content = ev.content or ""
                break

        if not content:
            return CompactionStats(
                tier=CompactionTier.SUMMARIZE,
                before_tokens=0,
                after_tokens=0,
                ratio_before=0.0,
                ratio_after=0.0,
                error="LLM returned empty summary content",
            )

        # 5. Remove old summary (if exists) before inserting the new one
        if prev is not None:
            messages.pop(prev_idx)

        # 6. Insert new summary at canonical position
        insert_idx = 1 if (messages and messages[0].get("role") == "system") else 0
        messages.insert(insert_idx, {
            "role": "assistant",
            "content": content,
            SUMMARY_MARKER_KEY: True,
        })

        return CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=0,    # filled by maybe_compact
            after_tokens=0,
            ratio_before=0.0,   # filled by maybe_compact
            ratio_after=0.0,
            summarized=True,
            summary_index=insert_idx,
        )
    except Exception as e:  # noqa: BLE001 — Tier 3 fail-soft
        return CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=0,
            after_tokens=0,
            ratio_before=0.0,
            ratio_after=0.0,
            error=str(e),
        )


# Orchestrator ---------------------------------------------------------------


def _noop_stats(
    messages: list[dict],
    counter: TokenCounter,
    tool_specs: list[dict] | None,
    config: ContextConfig,
) -> CompactionStats:
    total = sum(counter.categorize(messages, tool_specs).values())
    ratio = total / config.context_window if config.context_window else 0.0
    return CompactionStats(
        tier=CompactionTier.NONE,
        before_tokens=total,
        after_tokens=total,
        ratio_before=ratio,
        ratio_after=ratio,
    )


async def maybe_compact(
    messages: list[dict],
    tool_specs: list[dict] | None,
    counter: TokenCounter,
    config: ContextConfig,
    llm: Any = None,
) -> CompactionStats:
    """Run the tier cascade in place on ``messages`` before each LLM call.

    Short-circuits as soon as the post-tier ratio drops below the next
    threshold. Any exception is caught and surfaced via ``CompactionStats.error``
    — this function never raises (compaction must not kill the ReAct loop).
    """
    if not config.enabled:
        return _noop_stats(messages, counter, tool_specs, config)

    before = after = 0
    ratio = 0.0
    snapshot: list[dict] | None = None
    try:
        before = sum(counter.categorize(messages, tool_specs).values())
        ratio = before / config.context_window

        if ratio < config.tier1_threshold:
            return CompactionStats(
                tier=CompactionTier.NONE,
                before_tokens=before,
                after_tokens=before,
                ratio_before=ratio,
                ratio_after=ratio,
            )

        protect_until = find_protect_boundary(
            messages, counter, config.protect_zone_tokens
        )
        if protect_until == 0 or protect_until >= len(messages):
            return CompactionStats(
                tier=CompactionTier.NONE,
                before_tokens=before,
                after_tokens=before,
                ratio_before=ratio,
                ratio_after=ratio,
            )

        # Tier 1: Snip
        snipped = apply_tier1_snip(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier2_threshold:
            return CompactionStats(
                tier=CompactionTier.SNIP,
                before_tokens=before,
                after_tokens=after,
                ratio_before=ratio,
                ratio_after=after / config.context_window,
                messages_snip=snipped,
            )

        # Tier 2: Prune
        pruned_tool, truncated_asst = apply_tier2_prune(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier3_threshold:
            return CompactionStats(
                tier=CompactionTier.PRUNE,
                before_tokens=before,
                after_tokens=after,
                ratio_before=ratio,
                ratio_after=after / config.context_window,
                messages_snip=snipped,
                messages_prune=pruned_tool,
                messages_assistant_truncated=truncated_asst,
            )

        # Tier 3: Summarize
        stats = await apply_tier3_summarize(
            messages, protect_until, config, llm, counter=counter
        )
        after = sum(counter.categorize(messages, tool_specs).values())
        stats.before_tokens = before
        stats.after_tokens = after
        stats.ratio_before = ratio
        stats.ratio_after = after / config.context_window
        return stats

    except Exception as e:  # noqa: BLE001 — spec mandates fail-soft
        if snapshot is None:
            snapshot = [dict(m) for m in messages]
        return CompactionStats(
            tier=CompactionTier.NONE,
            before_tokens=before,
            after_tokens=after,
            ratio_before=ratio,
            ratio_after=ratio,
            error=str(e),
            before_snapshot=snapshot,
        )
