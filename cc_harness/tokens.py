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

    @classmethod
    def from_api(cls, usage: Any) -> "UsageRecord | None":
        if usage is None:
            return None
        return cls(
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        )

    def __add__(self, other: "UsageRecord") -> "UsageRecord":
        return UsageRecord(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
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

    def count_text(self, text: str | None) -> int:
        if not text:
            return 0
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
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    # Metadata
    iter_count: int = 0
    api_reported: bool = False
    tool_call_log: list = field(default_factory=list)  # [{name, args, ok, result}], Plan1 收集
    compaction: Any = None  # Plan3: CompactionStats obj (context.py) or None

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
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    iters_total: int = 0
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

    def add(self, turn: TurnTokenStats) -> None:
        self.turns += 1
        self.user_input += turn.user_input
        self.tool_calls += turn.tool_calls
        self.llm_output += turn.llm_output
        self.system_prompt += turn.system_prompt
        self.summary += turn.summary
        self.tool_definitions += turn.tool_definitions
        self.api_prompt_tokens += turn.api_prompt_tokens
        self.api_completion_tokens += turn.api_completion_tokens
        self.api_total_tokens += turn.api_total_tokens
        self.iters_total += turn.iter_count
        if turn.api_reported:
            self.turns_with_usage += 1
