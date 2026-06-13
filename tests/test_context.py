"""Tests for cc_harness.context — protect boundary, tiers, orchestrator."""
from dataclasses import dataclass
import pytest
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


# --- Tier 3: apply_tier3_summarize tests ---


@dataclass
class FakeSummarizerLLM:
    """Records each chat() call and returns a pre-programmed summary."""
    responses: list[str]
    call_count: int = 0
    last_tools: list | None = None

    async def chat(self, messages, tools):
        idx = self.call_count
        self.call_count += 1
        self.last_tools = tools
        # Lazy import to avoid pulling llm into this test file's namespace
        from cc_harness.llm import StreamEvent
        content = self.responses[idx] if idx < len(self.responses) else "default summary"
        yield StreamEvent(
            kind="done", content=content, pending=[], finish_reason="stop",
        )


def test_find_previous_summary_returns_none_when_no_summary():
    from cc_harness.context import _find_previous_summary
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    idx, content = _find_previous_summary(msgs)
    assert idx is None
    assert content == ""


def test_find_previous_summary_returns_last_summary():
    from cc_harness.context import _find_previous_summary
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    # Summaries are inserted at index 1, so the most recent has the lowest index.
    # Simulate two Tier 3 calls: first inserts "summary 1" at idx 1, then
    # second inserts "summary 2" at idx 1, pushing "summary 1" to idx 2.
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "summary 2", SUMMARY_MARKER_KEY: True},
        {"role": "assistant", "content": "summary 1", SUMMARY_MARKER_KEY: True},
        {"role": "user", "content": "now"},
    ]
    idx, content = _find_previous_summary(msgs)
    assert idx == 1
    assert content == "summary 2"


@pytest.mark.asyncio
async def test_apply_tier3_summarize_creates_summary_message():
    from cc_harness.context import apply_tier3_summarize, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["NEW SUMMARY"])
    stats = await apply_tier3_summarize(msgs, protect_until=3, config=cfg, counter=counter, llm=llm)
    assert stats.tier == CompactionTier.SUMMARIZE
    assert stats.summarized is True
    # Summary inserted at index 1 (after system)
    assert msgs[1][SUMMARY_MARKER_KEY] is True
    assert "NEW SUMMARY" in msgs[1]["content"]
    assert stats.summary_index == 1


@pytest.mark.asyncio
async def test_apply_tier3_summarize_passes_tools_none_to_llm():
    """Tier 3 严禁 LLM 调用工具 — spec line 67-68。"""
    from cc_harness.context import apply_tier3_summarize
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["s"])
    await apply_tier3_summarize(msgs, protect_until=2, config=cfg, counter=counter, llm=llm)
    assert llm.last_tools is None


@pytest.mark.asyncio
async def test_apply_tier3_summarize_incremental_across_two_calls():
    """第二次调用应使用第一次插入的摘要作为 previous_summary。"""
    from cc_harness.context import apply_tier3_summarize, _find_previous_summary
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["FIRST SUMMARY", "SECOND SUMMARY"])
    stats1 = await apply_tier3_summarize(msgs, protect_until=3, config=cfg, counter=counter, llm=llm)
    # Add more messages
    msgs.append({"role": "user", "content": "q2"})
    msgs.append({"role": "assistant", "content": "a2"})
    await apply_tier3_summarize(msgs, protect_until=5, config=cfg, counter=counter, llm=llm)
    assert llm.call_count == 2
    # Second call's previous_summary_idx should match stats1.summary_index
    prev_idx, prev_content = _find_previous_summary(msgs)
    assert prev_idx == stats1.summary_index


@pytest.mark.asyncio
async def test_apply_tier3_summarize_llm_error_returns_stats_with_error():
    """LLM 失败不应 raise — 应返回 stats with error 字段(spec line 482)。"""
    from cc_harness.context import apply_tier3_summarize
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter

    class FailingLLM:
        async def chat(self, messages, tools):
            raise RuntimeError("LLM down")
            yield  # never reached, but makes it a generator

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    stats = await apply_tier3_summarize(msgs, protect_until=2, config=cfg, counter=counter, llm=FailingLLM())
    assert stats.error is not None
    assert "LLM down" in stats.error
    assert stats.summarized is False


@pytest.mark.asyncio
async def test_apply_tier3_summarize_summary_index_recoverable():
    """stats.summary_index 必须能通过 _find_previous_summary 找回(实施期约束 #2)。"""
    from cc_harness.context import apply_tier3_summarize, _find_previous_summary
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["S"])
    stats = await apply_tier3_summarize(msgs, protect_until=4, config=cfg, counter=counter, llm=llm)
    prev_idx, _ = _find_previous_summary(msgs)
    assert prev_idx == stats.summary_index


# --- maybe_compact orchestrator tests (Task 8) ---

@pytest.mark.asyncio
async def test_maybe_compact_no_op_when_disabled():
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [{"role": "user", "content": "x" * 10_000}]
    cfg = ContextConfig(enabled=False)
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=[])
    stats = await maybe_compact(msgs, [], counter, cfg, llm)
    assert stats.tier == CompactionTier.NONE
    assert msgs[0]["content"] == "x" * 10_000


@pytest.mark.asyncio
async def test_maybe_compact_no_op_below_tier1():
    """ratio < tier1 时不触发任何 tier,直接返 NONE。"""
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    cfg = ContextConfig(context_window=1_000_000)  # ratio well below 0.6
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=[])
    stats = await maybe_compact(msgs, [], counter, cfg, llm)
    assert stats.tier == CompactionTier.NONE


@pytest.mark.asyncio
async def test_maybe_compact_tier1_only():
    """ratio 落在 tier1-tier2 之间,只跑 Tier 1。"""
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    big_tool_content = "line\n" * 500
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "c1", "content": big_tool_content},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(
        context_window=1000,
        tier1_threshold=0.6, tier2_threshold=0.8, tier3_threshold=0.95,
        protect_zone_tokens=10,  # small so the tool message falls outside protect zone
    )
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=[])
    stats = await maybe_compact(msgs, [], counter, cfg, llm)
    # Tier 1 should have run; tier 2 may or may not depending on exact ratio
    assert stats.tier in (CompactionTier.SNIP, CompactionTier.PRUNE)


@pytest.mark.asyncio
async def test_maybe_compact_exception_returns_stats_with_error():
    """任何内部异常都被 try/except 捕获,返 stats with error,不 raise。"""
    from cc_harness.context import maybe_compact
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 100_000},
    ]
    cfg = ContextConfig(context_window=1000)

    class Boom:
        def categorize(self, messages, tools=None):
            raise RuntimeError("categorize failed")

    counter = Boom()
    llm = FakeSummarizerLLM(responses=[])
    stats = await maybe_compact(msgs, [], counter, cfg, llm)
    assert stats.error is not None
    assert "categorize failed" in stats.error
    assert stats.before_snapshot is not None  # snapshot populated in error branch
    assert len(stats.before_snapshot) == 2


@pytest.mark.asyncio
async def test_maybe_compact_exception_does_not_raise():
    """失败必须不 raise — 压缩是额外保险,不能让 run_turn 主循环崩。"""
    from cc_harness.context import maybe_compact
    from cc_harness.config import ContextConfig
    msgs = [{"role": "user", "content": "x"}]

    class Boom:
        def categorize(self, messages, tools=None):
            raise ValueError("nope")

    cfg = ContextConfig(context_window=10)
    stats = await maybe_compact(msgs, [], Boom(), cfg, FakeSummarizerLLM(responses=[]))
    assert stats.error is not None
