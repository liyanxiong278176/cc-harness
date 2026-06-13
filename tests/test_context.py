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


# --- Tier 1: apply_tier1_snip tests ---

def test_apply_tier1_snip_truncates_long_tool_output():
    from cc_harness.context import apply_tier1_snip, CompactionTier
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "thinking"},
        {"role": "tool", "tool_call_id": "c1", "content": "\n".join(f"line {i}" for i in range(100))},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(snip_head_lines=2, snip_tail_lines=1)
    stats = apply_tier1_snip(msgs, protect_until=4, cfg=cfg)
    assert stats.tier == CompactionTier.SNIP
    assert stats.messages_snip == 1
    assert "line 0" in msgs[3]["content"]
    assert "line 1" in msgs[3]["content"]
    assert "line 99" in msgs[3]["content"]
    assert "line 50" not in msgs[3]["content"]
    assert "omitted" in msgs[3]["content"]

def test_apply_tier1_snip_truncates_user_code_blocks():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "user", "content": "请看:\n```python\n" + "\n".join(f"x_{i} = {i}" for i in range(50)) + "\n```\n谢谢"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(snip_head_lines=2, snip_tail_lines=1)
    apply_tier1_snip(msgs, protect_until=3, cfg=cfg)
    assert "请看" in msgs[2]["content"]
    assert "谢谢" in msgs[2]["content"]
    assert "x_0" in msgs[2]["content"]
    assert "x_49" in msgs[2]["content"]
    assert "x_25" not in msgs[2]["content"]
    assert "omitted" in msgs[2]["content"]

def test_apply_tier1_snip_does_not_touch_assistant_content():
    """Tier 1 永远不截 assistant 消息(content)— 保守处理,避免破坏思考连贯性。"""
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    long_text = "thinking... " * 500
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": long_text},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier1_snip(msgs, protect_until=3, cfg=cfg)
    assert msgs[2]["content"] == long_text

def test_apply_tier1_snip_skips_protected_tools():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "skill_call_1", "type": "function", "function": {"name": "skill_run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "skill_call_1", "content": "huge\n" * 1000},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(protected_tool_patterns=[r"^skill_"])
    original = msgs[3]["content"]
    apply_tier1_snip(msgs, protect_until=4, cfg=cfg)
    assert msgs[3]["content"] == original

def test_apply_tier1_snip_short_content_no_op():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "line 0\nline 1"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(snip_head_lines=2, snip_tail_lines=1)
    apply_tier1_snip(msgs, protect_until=3, cfg=cfg)
    assert msgs[2]["content"] == "line 0\nline 1"

def test_apply_tier1_snip_does_not_delete_messages():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "huge\n" * 1000},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier1_snip(msgs, protect_until=3, cfg=cfg)
    assert len(msgs) == 4


# --- Tier 2: apply_tier2_prune tests ---

def test_apply_tier2_prune_replaces_tool_output_with_placeholder():
    from cc_harness.context import apply_tier2_prune, TIER2_TOOL_PLACEHOLDER
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "huge result here"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, config=cfg)
    assert msgs[2]["content"] == TIER2_TOOL_PLACEHOLDER

def test_apply_tier2_prune_truncates_assistant_text():
    from cc_harness.context import apply_tier2_prune, TIER2_ASSISTANT_TRUNCATION_NOTICE
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "First sentence. Second sentence. Third sentence."},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, config=cfg)
    truncated = msgs[2]["content"]
    assert "First sentence" in truncated
    assert TIER2_ASSISTANT_TRUNCATION_NOTICE in truncated
    assert "Third sentence" not in truncated

def test_apply_tier2_prune_does_not_delete_tool_messages():
    """Tier 2 替换 content 但不删消息 — 保护 OpenAI tool_use/tool_result 配对。"""
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "x"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, config=cfg)
    assert len(msgs) == 4
    assert any(m.get("role") == "tool" for m in msgs)

def test_apply_tier2_prune_skips_summary_message():
    """带 _compaction_summary 标记的 assistant 消息不应被 Tier 2 截断。"""
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    summary = "This is a previous summary, it must not be truncated."
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": summary, SUMMARY_MARKER_KEY: True},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=2, config=cfg)
    assert msgs[1]["content"] == summary

def test_apply_tier2_prune_skips_protect_zone():
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "keep me"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=2, config=cfg)  # msgs[2:] is protect zone (index 2+ not touched)
    assert msgs[2]["content"] == "keep me"

def test_apply_tier2_prune_skips_protected_tools():
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "skill_call_1", "type": "function", "function": {"name": "skill_run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "skill_call_1", "content": "preserve me"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(protected_tool_patterns=[r"^skill_"])
    apply_tier2_prune(msgs, protect_until=4, config=cfg)
    assert msgs[3]["content"] == "preserve me"
