"""Tests for eval.metrics.schema dataclasses (Task 3.1)."""
from dataclasses import asdict
from eval.metrics.schema import (
    IterSnapshot, TaskMetrics, SessionMetrics, ComparisonReport,
)


def test_iter_snapshot_serializes_to_dict():
    s = IterSnapshot(
        iter_index=0, bucket_system_prompt=100, bucket_user_input=20,
        bucket_tool_calls=0, bucket_llm_output=50,
        bucket_tool_definitions=200, bucket_summary=0,
        total_tokens=370, ratio=0.0037, compaction_tier="NONE",
        tokens_saved_this_iter=0,
    )
    d = asdict(s)
    assert d["compaction_tier"] == "NONE"
    assert d["total_tokens"] == 370


def test_task_metrics_defaults_for_master_branch():
    """Master branch has no compaction — fields default to 0/empty."""
    tm = TaskMetrics(
        task_id="t1", task_index=0, level=1, branch="master",
        final_answer="42", ground_truth="42", is_correct=True,
        failed=False, failure_reason=None, per_iter_snapshots=[],
        bucket_system_prompt=0, bucket_user_input=0, bucket_tool_calls=0,
        bucket_llm_output=0, bucket_tool_definitions=0, bucket_summary=0,
        peak_total_tokens=0, peak_ratio=0.0, overflow=False,
        compactions_in_task=0, tier1_count=0, tier2_count=0, tier3_count=0,
        tokens_saved_in_task=0, summarize_llm_overhead_tokens=0,
        api_prompt_tokens=0, api_completion_tokens=0, api_total_tokens=0,
        iter_count=0, wall_time_seconds=0.0,
    )
    assert tm.branch == "master"
    assert tm.compactions_in_task == 0


def test_session_metrics_aggregate_fields():
    sm = SessionMetrics(
        branch="cc", started_at="2026-06-14T10:00:00", finished_at="...",
        git_commit="abc123", config_snapshot={"context_window": 200000},
        tasks_total=30, tasks_correct=18, tasks_failed=0, tasks_tool_unavailable=0,
        accuracy=0.6, peak_total_tokens_overall=500000, peak_ratio_overall=0.5,
        overflow_count=0, compactions_total=10, tier1_total=8, tier2_total=2,
        tier3_total=0, tokens_saved_total=12000, summarize_llm_overhead_total=0,
        peak_ratio_p50=0.2, peak_ratio_p95=0.45, tokens_saved_p50=100,
        tokens_saved_p95=2000, api_total_tokens_sum=1_000_000, iter_count_sum=200,
        wall_time_seconds_total=600.0,
    )
    assert sm.accuracy == 0.6
