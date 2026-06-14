"""Build TaskMetrics from TurnTokenStats + per-iter snapshots.

Public API:
- `collect_task_metrics` (Task 3.2/3.3)
- `reconstruct_iter_snapshots` (Task 3.4)
- `aggregate_session_metrics` (Task 3.5)
- `compare_sessions` (Task 3.6)
- `build_per_task_diffs` (Task 3.6)
"""
from __future__ import annotations
import statistics
from eval.datasets.gaia_loader import GaiaTask
from eval.metrics.schema import (
    IterSnapshot,
    SessionMetrics,
    TaskMetrics,
)


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
        summarize_llm_overhead_tokens=0,
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
    """
    snapshots: list[IterSnapshot] = []
    iter_idx = 0
    for end_idx in range(prefix_before_task + 1, len(messages) + 1):
        if messages[end_idx - 1].get("role") != "assistant":
            continue
        cats = counter.categorize(messages[:end_idx], tools=tools)
        total = sum(cats.values())
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


def aggregate_session_metrics(
    task_metrics, *,
    branch: str, started_at: str, finished_at: str, git_commit: str,
    config_snapshot: dict, tool_unavailable_count: int,
) -> SessionMetrics:
    n = len(task_metrics)
    correct = sum(1 for t in task_metrics if t.is_correct)
    failed = sum(1 for t in task_metrics if t.failed)
    runnable = n
    accuracy = (correct / runnable) if runnable else 0.0

    def _q(vals, q):
        return statistics.quantiles(vals, n=100)[q - 1] if len(vals) >= 2 else (vals[0] if vals else 0)

    peak_ratios = [t.peak_ratio for t in task_metrics] or [0.0]
    saved = [t.tokens_saved_in_task for t in task_metrics] or [0]
    peaks = [t.peak_total_tokens for t in task_metrics] or [0]

    return SessionMetrics(
        branch=branch, started_at=started_at, finished_at=finished_at,
        git_commit=git_commit, config_snapshot=config_snapshot,
        tasks_total=n, tasks_correct=correct, tasks_failed=failed,
        tasks_tool_unavailable=tool_unavailable_count, accuracy=accuracy,
        peak_total_tokens_overall=max(peaks),
        peak_ratio_overall=max(peak_ratios),
        overflow_count=sum(1 for t in task_metrics if t.overflow),
        compactions_total=sum(t.compactions_in_task for t in task_metrics),
        tier1_total=sum(t.tier1_count for t in task_metrics),
        tier2_total=sum(t.tier2_count for t in task_metrics),
        tier3_total=sum(t.tier3_count for t in task_metrics),
        tokens_saved_total=sum(saved),
        summarize_llm_overhead_total=sum(t.summarize_llm_overhead_tokens for t in task_metrics),
        peak_ratio_p50=_q(peak_ratios, 50), peak_ratio_p95=_q(peak_ratios, 95),
        tokens_saved_p50=int(_q(saved, 50)), tokens_saved_p95=int(_q(saved, 95)),
        api_total_tokens_sum=sum(t.api_total_tokens for t in task_metrics),
        iter_count_sum=sum(t.iter_count for t in task_metrics),
        wall_time_seconds_total=sum(t.wall_time_seconds for t in task_metrics),
    )


def compare_sessions(master, cc):
    from eval.metrics.schema import ComparisonReport
    api_delta = cc.api_total_tokens_sum - master.api_total_tokens_sum
    api_pct = (100.0 * api_delta / master.api_total_tokens_sum) if master.api_total_tokens_sum else 0.0
    return ComparisonReport(
        master=master, cc=cc,
        accuracy_delta=cc.accuracy - master.accuracy,
        peak_ratio_delta=cc.peak_ratio_overall - master.peak_ratio_overall,
        api_tokens_delta=api_delta, api_tokens_delta_pct=api_pct,
        overflow_delta=cc.overflow_count - master.overflow_count,
        per_task_diffs=[],
    )


def build_per_task_diffs(
    master_tms, cc_tms,
) -> list[dict]:
    """Pair task metrics by task_id; emit one dict per pair (or singleton if one branch missing)."""
    by_id_master = {t.task_id: t for t in master_tms}
    by_id_cc = {t.task_id: t for t in cc_tms}
    all_ids = sorted(by_id_master.keys() | by_id_cc.keys())
    out = []
    for tid in all_ids:
        m = by_id_master.get(tid)
        c = by_id_cc.get(tid)
        out.append({
            "task_id": tid,
            "level": (m or c).level,
            "master_correct": m.is_correct if m else None,
            "cc_correct": c.is_correct if c else None,
            "master_failed": m.failed if m else None,
            "cc_failed": c.failed if c else None,
            "master_peak": m.peak_total_tokens if m else None,
            "cc_peak": c.peak_total_tokens if c else None,
            "master_api_tokens": m.api_total_tokens if m else None,
            "cc_api_tokens": c.api_total_tokens if c else None,
        })
    return out
