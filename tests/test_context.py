"""Tests for cc_harness.context — protect boundary, tiers, orchestrator."""
from cc_harness.tokens import TokenCounter


def test_find_protect_boundary_empty_messages_returns_zero():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    assert find_protect_boundary([], counter, budget_tokens=1000) == 0

def test_find_protect_boundary_only_system_returns_zero():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [{"role": "system", "content": "sys"}]
    assert find_protect_boundary(msgs, counter, budget_tokens=1000) == 0

def test_find_protect_boundary_single_user_message_clamps():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    # Last user at index 1, budget too small → clamp at 1
    assert find_protect_boundary(msgs, counter, budget_tokens=1) == 1

def test_find_protect_boundary_budget_covers_last_user():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "new question"},
    ]
    # Budget big enough to cover the last user message; should land at system (index 0)
    assert find_protect_boundary(msgs, counter, budget_tokens=10_000) == 0

def test_find_protect_boundary_budget_too_small_clamps_at_last_user():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 10_000},
    ]
    # Even budget=1, clamp at last user message (index 1)
    assert find_protect_boundary(msgs, counter, budget_tokens=1) == 1
