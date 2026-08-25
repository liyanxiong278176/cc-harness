"""Token counting, categorization, and turn/session statistics.

Provides:
- `UsageRecord`: wraps a single API-reported usage snapshot.
- `TokenCounter`: tiktoken-backed 6-bucket categorizer for OpenAI message lists.
- `TurnTokenStats`: aggregate of one ReAct turn (1..N LLM calls).
- `SessionTokenStats`: cross-turn session totals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Plan3: marker on assistant messages holding a compaction summary. Such messages
# bucket into `summary` (not `llm_output`). Defined here (leaf module, zero
# cc_harness imports) — prompts.py imports it; putting it there would cycle.
SUMMARY_MARKER_KEY = "_compaction_summary"


@dataclass(frozen=True)
class UsageRecord:
    """One LLM call's API-reported usage. Immutable; supports `+` for summing."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_read_prompt_tokens: int = 0
    cache_creation_prompt_tokens: int = 0
    reported_cost: float | None = None
    reported_cost_currency: str | None = None

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(
            0,
            self.prompt_tokens
            - self.cache_read_prompt_tokens
            - self.cache_creation_prompt_tokens,
        )

    @property
    def cache_miss_prompt_tokens(self) -> int:
        """Normalized cache misses (DeepSeek calls this prompt_cache_miss_tokens)."""
        return self.uncached_prompt_tokens

    @classmethod
    def from_api(cls, usage: Any) -> UsageRecord | None:
        if usage is None:
            return None

        def value(obj: Any, key: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        def nonnegative_int(raw: Any) -> int:
            try:
                return max(0, int(raw or 0))
            except (TypeError, ValueError):
                return 0

        prompt_tokens = nonnegative_int(value(usage, "prompt_tokens", 0))
        details = value(usage, "prompt_tokens_details", None)
        cache_read = value(usage, "prompt_cache_hit_tokens", None)
        if cache_read is None:
            cache_read = value(details, "cached_tokens", 0)
        cache_creation = value(usage, "cache_creation_input_tokens", None)
        if cache_creation is None:
            cache_creation = value(usage, "cache_write_tokens", None)
        if cache_creation is None:
            cache_creation = value(details, "cache_write_tokens", 0)
        reported_cost = value(usage, "cost", None)
        if reported_cost is None:
            reported_cost = value(usage, "total_cost", None)
        try:
            normalized_cost = float(reported_cost) if reported_cost is not None else None
        except (TypeError, ValueError):
            normalized_cost = None
        currency = value(usage, "cost_currency", None) or value(usage, "currency", None)
        if currency is not None:
            currency = str(currency)
        cache_read = min(prompt_tokens, nonnegative_int(cache_read))
        cache_creation = min(prompt_tokens - cache_read, nonnegative_int(cache_creation))
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=nonnegative_int(value(usage, "completion_tokens", 0)),
            total_tokens=nonnegative_int(value(usage, "total_tokens", 0)),
            cache_read_prompt_tokens=cache_read,
            cache_creation_prompt_tokens=cache_creation,
            reported_cost=normalized_cost,
            reported_cost_currency=currency,
        )

    def __add__(self, other: UsageRecord) -> UsageRecord:
        if self.reported_cost is None or other.reported_cost is None:
            cost = None
        elif self.reported_cost_currency not in (None, other.reported_cost_currency):
            cost = None
        else:
            cost = self.reported_cost + other.reported_cost
        return UsageRecord(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_read_prompt_tokens=(
                self.cache_read_prompt_tokens + other.cache_read_prompt_tokens
            ),
            cache_creation_prompt_tokens=(
                self.cache_creation_prompt_tokens + other.cache_creation_prompt_tokens
            ),
            reported_cost=cost,
            reported_cost_currency=(
                (self.reported_cost_currency or other.reported_cost_currency)
                if cost is not None else None
            ),
        )


class TokenCounter:
    """Categorize an OpenAI-format messages list (+ optional tools) into 6 token buckets.

    Default encoding: cl100k_base (works for GPT-4/3.5, DeepSeek-V2/V3).
    For GPT-4o, pass encoding_name="o200k_base".
    """

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken
        try:
            self._enc = tiktoken.get_encoding(encoding_name)
        except ValueError as e:
            raise ValueError(f"unknown tiktoken encoding: {encoding_name!r}") from e
        self._encoding_name = encoding_name

    def count_text(self, text: Any) -> int:
        if not text:
            return 0
        if isinstance(text, list):
            text = "\n".join(
                str(part.get("text", ""))
                for part in text
                if isinstance(part, dict) and part.get("type") == "text"
            )
        elif not isinstance(text, str):
            text = str(text)
        return len(self._enc.encode(text))

    def categorize(
        self, messages: list[dict], tools: list[dict] | None = None,
    ) -> dict[str, int]:
        """Walk messages (+ optional tool schemas) and bucket tokens into 6 categories.

        - system_prompt:    role=system content
        - user_input:       role=user content
        - tool_calls:       role=tool content + assistant tool_calls field
        - llm_output:       assistant content (text only, NOT a compaction summary)
        - summary:          assistant content flagged `_compaction_summary` (Plan3)
        - tool_definitions: JSON-serialized `tools` parameter (sent every API call)
        """
        system_prompt = user_input = tool_calls = llm_output = summary = 0
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_prompt += self.count_text(m.get("content"))
            elif role == "user":
                user_input += self.count_text(m.get("content"))
            elif role == "tool":
                tool_calls += self.count_text(m.get("content"))
            elif role == "assistant":
                content = m.get("content")
                if m.get(SUMMARY_MARKER_KEY):  # Plan3: summary → own bucket
                    summary += self.count_text(content)
                elif content:
                    llm_output += self.count_text(content)
                for tc in (m.get("tool_calls") or []):
                    tool_calls += self.count_text(json.dumps(tc, ensure_ascii=False))
            # unknown roles: silently skip

        tool_definitions = 0
        if tools:
            for tool in tools:
                tool_definitions += self.count_text(json.dumps(tool, ensure_ascii=False))

        return {
            "user_input": user_input,
            "tool_calls": tool_calls,
            "llm_output": llm_output,
            "system_prompt": system_prompt,
            "summary": summary,
            "tool_definitions": tool_definitions,
        }


@dataclass
class TurnTokenStats:
    """Aggregate of one run_turn call (1..N LLM calls in ReAct loop).

    6-category breakdown is computed by TokenCounter over the final messages
    list + tool schemas (tiktoken-based, may have small drift vs API total).
    API fields are summed across iters (authoritative billable count).
    """
    # 6-category breakdown (tiktoken)
    user_input: int = 0
    tool_calls: int = 0
    llm_output: int = 0
    system_prompt: int = 0
    summary: int = 0
    tool_definitions: int = 0
    # API-reported (sum across iters in this turn)
    api_prompt_tokens: int = 0
    api_uncached_prompt_tokens: int = 0
    api_cache_read_prompt_tokens: int = 0
    api_cache_creation_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    api_reported_cost: float | None = None
    api_reported_cost_currency: str | None = None
    # Metadata
    iter_count: int = 0
    auxiliary_model_calls: int = 0
    api_reported: bool = False
    tool_call_log: list = field(default_factory=list)  # [{name, args, ok, result}], Plan1 收集
    compaction: Any = None  # Plan3: CompactionStats obj (context.py) or None
    error: str | None = None  # D1 Task 4 fix:run_turn fatal error message(if any)
    prompt_metadata: dict | None = None  # safe version/digest only; never prompt text

    @property
    def breakdown_subtotal(self) -> int:
        return (
            self.user_input
            + self.tool_calls
            + self.llm_output
            + self.system_prompt
            + self.summary
            + self.tool_definitions
        )

    @property
    def api_vs_breakdown_drift_pct(self) -> float:
        if self.api_total_tokens == 0:
            return 0.0
        return 100.0 * (self.breakdown_subtotal - self.api_total_tokens) / self.api_total_tokens


@dataclass
class SessionTokenStats:
    """Whole REPL session totals, summed across turns."""
    turns: int = 0
    user_input: int = 0
    tool_calls: int = 0
    llm_output: int = 0
    system_prompt: int = 0
    summary: int = 0
    tool_definitions: int = 0
    api_prompt_tokens: int = 0
    api_uncached_prompt_tokens: int = 0
    api_cache_read_prompt_tokens: int = 0
    api_cache_creation_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    api_reported_cost: float | None = None
    api_reported_cost_currency: str | None = None
    # ``api_cost_complete`` is false once any observed API usage omitted a
    # provider-reported cost.  We never infer a price from token counts.
    api_cost_complete: bool = True
    api_cost_observed: bool = False
    iters_total: int = 0
    auxiliary_model_calls: int = 0
    turns_with_usage: int = 0

    @property
    def breakdown_subtotal(self) -> int:
        return (
            self.user_input
            + self.tool_calls
            + self.llm_output
            + self.system_prompt
            + self.summary
            + self.tool_definitions
        )

    def add(
        self,
        turn: TurnTokenStats,
        *,
        messages: list[dict] | None = None,
        counter: TokenCounter | None = None,
        tools: list[dict] | None = None,
    ) -> None:
        """Merge a turn's stats into the session totals.

        Finding 7 fix:the 6 token breakdown buckets (user_input, tool_calls,
        llm_output, system_prompt, summary, tool_definitions) describe the
        CURRENT history state — recomputing them from current messages each
        turn is correct. Additive ``+= turn.foo`` would double-count (each
        turn's stats already include the full history snapshot, so summing
        across turns multiplies by turn count).

        When caller passes ``messages`` + ``counter``, the breakdown buckets
        are REPLACED (not added) with the recomputed values. API fields
        (api_prompt_tokens / api_completion_tokens / api_total_tokens /
        iters_total / turns_with_usage) remain additive — they are per-call
        stats that genuinely accumulate across the session.

        Backward compat: when caller omits messages/counter, the original
        additive behavior is preserved (used by older tests).
        """
        self.turns += 1
        if messages is not None and counter is not None:
            cats = counter.categorize(messages, tools=tools)
            self.user_input = cats["user_input"]
            self.tool_calls = cats["tool_calls"]
            self.llm_output = cats["llm_output"]
            self.system_prompt = cats["system_prompt"]
            self.summary = cats["summary"]
            self.tool_definitions = cats["tool_definitions"]
        else:
            # legacy additive fallback (preserves old behavior)
            self.user_input += turn.user_input
            self.tool_calls += turn.tool_calls
            self.llm_output += turn.llm_output
            self.system_prompt += turn.system_prompt
            self.summary += turn.summary
            self.tool_definitions += turn.tool_definitions
        # API fields are per-LLM-call totals and always add.
        self.api_prompt_tokens += turn.api_prompt_tokens
        self.api_uncached_prompt_tokens += turn.api_uncached_prompt_tokens
        self.api_cache_read_prompt_tokens += turn.api_cache_read_prompt_tokens
        self.api_cache_creation_prompt_tokens += turn.api_cache_creation_prompt_tokens
        self.api_completion_tokens += turn.api_completion_tokens
        self.api_total_tokens += turn.api_total_tokens
        if turn.api_reported:
            self.api_cost_observed = True
            if turn.api_reported_cost is None or not self.api_cost_complete:
                self.api_cost_complete = False
                self.api_reported_cost = None
                self.api_reported_cost_currency = None
            elif self.api_reported_cost_currency not in (None, turn.api_reported_cost_currency):
                self.api_cost_complete = False
                self.api_reported_cost = None
                self.api_reported_cost_currency = None
            else:
                self.api_reported_cost = (self.api_reported_cost or 0.0) + turn.api_reported_cost
                self.api_reported_cost_currency = (
                    self.api_reported_cost_currency or turn.api_reported_cost_currency
                )
        self.iters_total += turn.iter_count
        self.auxiliary_model_calls += turn.auxiliary_model_calls
        if turn.api_reported:
            self.turns_with_usage += 1
