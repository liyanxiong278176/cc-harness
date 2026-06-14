"""Tests for eval.reports.markdown (Task 5.1)."""
from eval.metrics.schema import SessionMetrics, ComparisonReport
from eval.reports.markdown import render_comparison_report


def _sm(**overrides):
    defaults = dict(
        branch="b", started_at="2026-06-14T10:00:00Z",
        finished_at="2026-06-14T10:05:00Z",
        git_commit="abc1234", config_snapshot={"context_window": 200000},
        tasks_total=30, tasks_correct=20, tasks_failed=0,
        tasks_tool_unavailable=0, accuracy=0.6667,
        peak_total_tokens_overall=500_000, peak_ratio_overall=2.5,
        overflow_count=0, compactions_total=10, tier1_total=8,
        tier2_total=2, tier3_total=0, tokens_saved_total=12000,
        summarize_llm_overhead_total=0, peak_ratio_p50=1.2,
        peak_ratio_p95=2.3, tokens_saved_p50=100, tokens_saved_p95=2000,
        api_total_tokens_sum=1_000_000, iter_count_sum=200,
        wall_time_seconds_total=600.0,
    )
    defaults.update(overrides)
    return SessionMetrics(**defaults)


def test_report_header_and_summary_table_present():
    cmp = ComparisonReport(
        master=_sm(branch="master", accuracy=0.6, overflow_count=5,
                   api_total_tokens_sum=1_500_000),
        cc=_sm(branch="context-compaction", accuracy=0.55, overflow_count=0,
               api_total_tokens_sum=1_000_000),
        accuracy_delta=-0.05, peak_ratio_delta=-1.0,
        api_tokens_delta=-500_000, api_tokens_delta_pct=-33.3,
        overflow_delta=-5, per_task_diffs=[],
    )
    md = render_comparison_report(cmp)
    assert "# GAIA Context Eval" in md
    assert "TL;DR" in md
    assert "Accuracy" in md
    assert "−33.3%" in md or "-33.3%" in md
    assert "master" in md and "context-compaction" in md
