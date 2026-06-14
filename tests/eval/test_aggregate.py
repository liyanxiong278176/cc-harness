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


def test_compare_sessions_computes_deltas():
    from eval.metrics.collector import compare_sessions, aggregate_session_metrics
    master = aggregate_session_metrics(
        [_tm(task_id="t1", is_correct=True, api_total_tokens=1000,
             peak_total_tokens=500, peak_ratio=0.25, overflow=False)],
        branch="master", started_at="", finished_at="",
        git_commit="m", config_snapshot={}, tool_unavailable_count=0,
    )
    cc = aggregate_session_metrics(
        [_tm(task_id="t1", is_correct=True, api_total_tokens=600,
             peak_total_tokens=300, peak_ratio=0.15, overflow=False)],
        branch="cc", started_at="", finished_at="",
        git_commit="c", config_snapshot={}, tool_unavailable_count=0,
    )
    cmp = compare_sessions(master, cc)
    assert cmp.api_tokens_delta == -400  # cc saved 400
    assert cmp.api_tokens_delta_pct == -40.0
    assert cmp.peak_ratio_delta == cc.peak_ratio_overall - master.peak_ratio_overall
    assert len(cmp.per_task_diffs) == 1
    assert cmp.per_task_diffs[0]["task_id"] == "t1"


def test_build_per_task_diffs():
    from eval.metrics.collector import build_per_task_diffs
    m = [_tm(task_id="t1", is_correct=True, peak_total_tokens=500)]
    c = [_tm(task_id="t1", is_correct=False, peak_total_tokens=300)]
    diffs = build_per_task_diffs(m, c)
    assert len(diffs) == 1
    assert diffs[0]["master_correct"] is True
    assert diffs[0]["cc_correct"] is False
    assert diffs[0]["master_peak"] == 500
