"""Build TaskMetrics from TurnTokenStats + per-iter snapshots."""
from __future__ import annotations
from eval.datasets.gaia_loader import GaiaTask
from eval.metrics.schema import IterSnapshot, TaskMetrics


def collect_task_metrics(
    *,
    task: GaiaTask,
    task_index: int,
    branch: str,
    turn_stats,                       # TurnTokenStats; duck-typed for master compat
    iter_snapshots: list[IterSnapshot],
    final_answer: str,
    is_correct: bool,
    failed: bool,
    failure_reason: str | None,
    wall_time_seconds: float,
    context_window: int,
) -> TaskMetrics:
    """Pure function. Handles missing `compaction` field on master.

    `turn_stats` may be either branch's TurnTokenStats; we use getattr for any
    field that exists only on context-compaction.
    """
    peak = max((s.total_tokens for s in iter_snapshots), default=0)
    peak_ratio = peak / context_window if context_window else 0.0
    overflow = peak_ratio > 1.0

    # Aggregate compaction across iters from snapshots (works for both branches;
    # master snapshots will all be NONE)
    tier1 = sum(1 for s in iter_snapshots if s.compaction_tier == "SNIP")
    tier2 = sum(1 for s in iter_snapshots if s.compaction_tier == "PRUNE")
    tier3 = sum(1 for s in iter_snapshots if s.compaction_tier == "SUMMARIZE")
    tokens_saved = sum(s.tokens_saved_this_iter for s in iter_snapshots)
    compactions = tier1 + tier2 + tier3

    return TaskMetrics(
        task_id=task.task_id, task_index=task_index, level=task.level, branch=branch,
        final_answer=final_answer, ground_truth=task.ground_truth,
        is_correct=is_correct, failed=failed, failure_reason=failure_reason,
        per_iter_snapshots=iter_snapshots,
        bucket_system_prompt=getattr(turn_stats, "system_prompt", 0),
        bucket_user_input=getattr(turn_stats, "user_input", 0),
        bucket_tool_calls=getattr(turn_stats, "tool_calls", 0),
        bucket_llm_output=getattr(turn_stats, "llm_output", 0),
        bucket_tool_definitions=getattr(turn_stats, "tool_definitions", 0),
        bucket_summary=getattr(turn_stats, "summary", 0),
        peak_total_tokens=peak, peak_ratio=peak_ratio, overflow=overflow,
        compactions_in_task=compactions,
        tier1_count=tier1, tier2_count=tier2, tier3_count=tier3,
        tokens_saved_in_task=tokens_saved,
        summarize_llm_overhead_tokens=0,  # populated by reconstruct (3.4)
        api_prompt_tokens=getattr(turn_stats, "api_prompt_tokens", 0),
        api_completion_tokens=getattr(turn_stats, "api_completion_tokens", 0),
        api_total_tokens=getattr(turn_stats, "api_total_tokens", 0),
        iter_count=getattr(turn_stats, "iter_count", 0),
        wall_time_seconds=wall_time_seconds,
    )


def reconstruct_iter_snapshots(
    *,
    messages: list[dict],
    tools: list[dict] | None,
    counter,                            # TokenCounter
    compaction_per_iter: list,          # list[CompactionStats]; [] for master
    context_window: int,
    prefix_before_task: int,
) -> list[IterSnapshot]:
    """Walk messages from prefix_before_task forward; emit one snapshot per
    assistant-message boundary (representing one ReAct iter completion).

    For each assistant boundary, categorize the prefix-so-far into 6 buckets
    via `counter.categorize`. If compaction_per_iter has an entry for that
    iter, tier + tokens_saved come from it; else NONE / 0.
    """
    snapshots: list[IterSnapshot] = []
    iter_idx = 0
    for end_idx in range(prefix_before_task + 1, len(messages) + 1):
        if messages[end_idx - 1].get("role") != "assistant":
            continue
        cats = counter.categorize(messages[:end_idx], tools=tools)
        total = sum(cats.values())
        # compaction stat for this iter (if any)
        if iter_idx < len(compaction_per_iter):
            comp = compaction_per_iter[iter_idx]
            tier_name = comp.tier.name if comp.tier else "NONE"
            saved = max(0, comp.before_tokens - comp.after_tokens)
        else:
            tier_name, saved = "NONE", 0
        snapshots.append(IterSnapshot(
            iter_index=iter_idx,
            bucket_system_prompt=cats.get("system_prompt", 0),
            bucket_user_input=cats.get("user_input", 0),
            bucket_tool_calls=cats.get("tool_calls", 0),
            bucket_llm_output=cats.get("llm_output", 0),
            bucket_tool_definitions=cats.get("tool_definitions", 0),
            bucket_summary=cats.get("summary", 0),
            total_tokens=total,
            ratio=total / context_window if context_window else 0.0,
            compaction_tier=tier_name,
            tokens_saved_this_iter=saved,
        ))
        iter_idx += 1
    return snapshots

