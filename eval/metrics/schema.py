"""Dataclasses for per-task / per-session / cross-branch metrics."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class IterSnapshot:
    iter_index: int
    bucket_system_prompt: int
    bucket_user_input: int
    bucket_tool_calls: int
    bucket_llm_output: int
    bucket_tool_definitions: int
    bucket_summary: int
    total_tokens: int
    ratio: float
    compaction_tier: str           # "NONE" | "SNIP" | "PRUNE" | "SUMMARIZE"
    tokens_saved_this_iter: int


@dataclass
class TaskMetrics:
    # Identity
    task_id: str
    task_index: int
    level: int
    branch: str

    # Outcome
    final_answer: str
    ground_truth: str
    is_correct: bool
    failed: bool
    failure_reason: str | None
        # context_overflow | llm_error | rate_limit | max_iter
        # | tool_unavailable | grader_error
    per_iter_snapshots: list[IterSnapshot] = field(default_factory=list)

    # End-of-task bucket totals
    bucket_system_prompt: int = 0
    bucket_user_input: int = 0
    bucket_tool_calls: int = 0
    bucket_llm_output: int = 0
    bucket_tool_definitions: int = 0
    bucket_summary: int = 0
    peak_total_tokens: int = 0
    peak_ratio: float = 0.0
    overflow: bool = False

    # Compaction (always zero on master)
    compactions_in_task: int = 0
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0
    tokens_saved_in_task: int = 0
    summarize_llm_overhead_tokens: int = 0

    # Cost / latency
    api_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    iter_count: int = 0
    wall_time_seconds: float = 0.0


@dataclass
class SessionMetrics:
    branch: str
    started_at: str
    finished_at: str
    git_commit: str
    config_snapshot: dict

    tasks_total: int
    tasks_correct: int
    tasks_failed: int
    tasks_tool_unavailable: int
    accuracy: float

    peak_total_tokens_overall: int
    peak_ratio_overall: float
    overflow_count: int

    compactions_total: int
    tier1_total: int
    tier2_total: int
    tier3_total: int
    tokens_saved_total: int
    summarize_llm_overhead_total: int

    peak_ratio_p50: float
    peak_ratio_p95: float
    tokens_saved_p50: int
    tokens_saved_p95: int

    api_total_tokens_sum: int
    iter_count_sum: int
    wall_time_seconds_total: float


@dataclass
class ComparisonReport:
    master: SessionMetrics
    cc: SessionMetrics
    accuracy_delta: float
    peak_ratio_delta: float
    api_tokens_delta: int
    api_tokens_delta_pct: float
    overflow_delta: int
    per_task_diffs: list[dict] = field(default_factory=list)
