"""Tests for eval.metrics.collector (Tasks 3.2/3.3/3.4)."""
from cc_harness.tokens import TurnTokenStats
from cc_harness.context import CompactionStats, CompactionTier
from eval.datasets.gaia_loader import GaiaTask
from eval.metrics.collector import collect_task_metrics
from eval.metrics.schema import IterSnapshot


def _task() -> GaiaTask:
    return GaiaTask("t1", "q", 1, "42", None)


def test_collect_with_compaction_stats():
    """Branch that has compaction populates tier counts + tokens_saved."""
    comp = CompactionStats(
        tier=CompactionTier.SNIP,
        before_tokens=1000, after_tokens=600,
        ratio_before=0.6, ratio_after=0.36,
        messages_snip=2,
    )
    stats = TurnTokenStats(
        user_input=100, tool_calls=50, llm_output=80,
        system_prompt=200, tool_definitions=170, summary=0,
        api_prompt_tokens=600, api_completion_tokens=100, api_total_tokens=700,
        iter_count=3, compaction=comp, api_reported=True,
    )
    snapshots = [
        IterSnapshot(
            iter_index=0, bucket_system_prompt=200, bucket_user_input=100,
            bucket_tool_calls=0, bucket_llm_output=0, bucket_tool_definitions=170,
            bucket_summary=0, total_tokens=470, ratio=0.0047,
            compaction_tier="NONE", tokens_saved_this_iter=0,
        ),
        IterSnapshot(
            iter_index=1, bucket_system_prompt=200, bucket_user_input=100,
            bucket_tool_calls=300, bucket_llm_output=80, bucket_tool_definitions=170,
            bucket_summary=0, total_tokens=850, ratio=0.0085,
            compaction_tier="SNIP", tokens_saved_this_iter=400,
        ),
    ]
    tm = collect_task_metrics(
        task=_task(), task_index=0, branch="context-compaction",
        turn_stats=stats, iter_snapshots=snapshots,
        final_answer="42", is_correct=True, failed=False, failure_reason=None,
        wall_time_seconds=12.5, context_window=200_000,
    )
    assert tm.tier1_count == 1
    assert tm.compactions_in_task == 1
    assert tm.tokens_saved_in_task == 400
    assert tm.peak_total_tokens == 850
    assert tm.api_total_tokens == 700
    assert tm.bucket_user_input == 100


def test_collect_handles_master_without_compaction_field():
    """Master's TurnTokenStats has no 'compaction' / no 'summary' bucket.

    We simulate by passing a SimpleNamespace lacking those fields.
    """
    from types import SimpleNamespace
    stats = SimpleNamespace(
        user_input=100, tool_calls=50, llm_output=80,
        system_prompt=200, tool_definitions=170,
        # no `summary`, no `compaction`
        api_prompt_tokens=600, api_completion_tokens=100, api_total_tokens=700,
        iter_count=3, api_reported=True,
    )
    snapshots = [IterSnapshot(
        iter_index=0, bucket_system_prompt=200, bucket_user_input=100,
        bucket_tool_calls=0, bucket_llm_output=0, bucket_tool_definitions=170,
        bucket_summary=0, total_tokens=470, ratio=0.005,
        compaction_tier="NONE", tokens_saved_this_iter=0,
    )]
    tm = collect_task_metrics(
        task=_task(), task_index=0, branch="master",
        turn_stats=stats, iter_snapshots=snapshots,
        final_answer="42", is_correct=True, failed=False, failure_reason=None,
        wall_time_seconds=5.0, context_window=200_000,
    )
    assert tm.bucket_summary == 0
    assert tm.tier1_count == 0
    assert tm.compactions_in_task == 0
    assert tm.api_total_tokens == 700


def test_reconstruct_snapshots_from_messages_only():
    """Master branch: no per-iter compaction data. Snapshots derived from
    walking messages and categorizing prefix-by-prefix.
    """
    from cc_harness.tokens import TokenCounter
    from eval.metrics.collector import reconstruct_iter_snapshots

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "thinking",
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
        {"role": "assistant", "content": "answer"},
    ]
    counter = TokenCounter()
    snaps = reconstruct_iter_snapshots(
        messages=messages, tools=[], counter=counter,
        compaction_per_iter=[],     # master: empty
        context_window=200_000,
        prefix_before_task=2,       # system + user already in
    )
    # 2 assistant boundaries → 2 snapshots
    assert len(snaps) == 2
    # ratios increase
    assert snaps[1].total_tokens >= snaps[0].total_tokens
    assert all(s.compaction_tier == "NONE" for s in snaps)
