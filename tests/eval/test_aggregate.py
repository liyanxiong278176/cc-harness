"""Tests for eval.metrics.collector aggregation (Tasks 3.5, 3.6)."""
from eval.metrics.collector import aggregate_session_metrics
from eval.metrics.schema import TaskMetrics


def _tm(**overrides):
    defaults = dict(
        task_id="t", task_index=0, level=1, branch="cc",
        final_answer="", ground_truth="", is_correct=True,
        failed=False, failure_reason=None,
    )
    defaults.update(overrides)
    return TaskMetrics(**defaults)


def test_aggregate_basic_counts():
    tms = [
        _tm(task_id="t1", is_correct=True, peak_total_tokens=100,
            peak_ratio=0.001, tier1_count=1, tokens_saved_in_task=50,
            api_total_tokens=300, iter_count=3, wall_time_seconds=5.0),
        _tm(task_id="t2", is_correct=False, peak_total_tokens=200,
            peak_ratio=0.002, tier2_count=1, tokens_saved_in_task=80,
            api_total_tokens=400, iter_count=4, wall_time_seconds=7.0),
    ]
    sm = aggregate_session_metrics(
        tms, branch="cc", started_at="t0", finished_at="t1",
        git_commit="sha", config_snapshot={"context_window": 200_000},
        tool_unavailable_count=0,
    )
    assert sm.tasks_total == 2
    assert sm.tasks_correct == 1
    assert sm.tasks_failed == 0
    assert sm.accuracy == 0.5
    assert sm.peak_total_tokens_overall == 200
    assert sm.tier1_total == 1
    assert sm.tier2_total == 1
    assert sm.tokens_saved_total == 130
    assert sm.api_total_tokens_sum == 700
    assert sm.wall_time_seconds_total == 12.0


def test_aggregate_excludes_tool_unavailable_from_accuracy():
    tms = [
        _tm(task_id="t1", is_correct=True),
        _tm(task_id="t2", is_correct=False),
    ]
    sm = aggregate_session_metrics(
        tms, branch="cc", started_at="t0", finished_at="t1",
        git_commit="sha", config_snapshot={},
        tool_unavailable_count=5,
    )
    # 1 correct / (2 runnable) = 0.5; the 5 unavail are tracked separately
    assert sm.accuracy == 0.5
    assert sm.tasks_tool_unavailable == 5
