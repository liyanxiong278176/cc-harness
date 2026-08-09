"""Tests for cc_harness/context.py — protect boundary + Tier1-3 + maybe_compact.

Plan3 Task4+5: 38+ tests (Tier3 Summarize + full cascade).
Per spec 2026-06-12 「test_context.py 38 test 分布」.
"""
import json

import pytest

from cc_harness.config import ContextConfig
from cc_harness.llm import StreamEvent
from cc_harness.tokens import SUMMARY_MARKER_KEY


# Lazy import — context.py created in Step 3; tests collected after.
def _import_context():
    from cc_harness.context import (
        CompactionTier,
        CompactionStats,
        find_protect_boundary,
        apply_tier1_snip,
        apply_tier2_prune,
        apply_tier3_summarize,
        _find_previous_summary,
        maybe_compact,
        TIER2_TOOL_PLACEHOLDER,
    )
    return (CompactionTier, CompactionStats, find_protect_boundary,
            apply_tier1_snip, apply_tier2_prune, apply_tier3_summarize,
            _find_previous_summary, maybe_compact, TIER2_TOOL_PLACEHOLDER)


class FakeCounter:
    """Deterministic counter: 1 token = 1 character. Mirrors 6-bucket categorize."""

    def count_text(self, text):
        if not text:
            return 0
        return len(text)

    def categorize(self, messages, tools=None):
        cats = {"user_input": 0, "tool_calls": 0, "llm_output": 0,
                "system_prompt": 0, "summary": 0, "tool_definitions": 0}
        for m in messages:
            role = m.get("role")
            c = m.get("content")
            t = self.count_text(c if isinstance(c, str) else "")
            if role == "system":
                cats["system_prompt"] += t
            elif role == "user":
                cats["user_input"] += t
            elif role == "tool":
                cats["tool_calls"] += t
            elif role == "assistant":
                if m.get(SUMMARY_MARKER_KEY):
                    cats["summary"] += t
                else:
                    cats["llm_output"] += t
            for tc in (m.get("tool_calls") or []):
                cats["tool_calls"] += self.count_text(json.dumps(tc, ensure_ascii=False))
        if tools:
            for tool in tools:
                cats["tool_definitions"] += self.count_text(json.dumps(tool, ensure_ascii=False))
        return cats


class FakeLLM:
    """Mock LLM for Tier 3 tests — yields a single done event with preset content.

    Mirrors ``LLMClient.chat``: async generator yielding ``StreamEvent``.
    Records ``last_messages`` / ``last_tools`` / ``call_count`` for assertions.
    """

    def __init__(self, content="mock summary", error=None):
        self.content = content
        self.error = error
        self.last_messages = None
        self.last_tools = "UNSET"  # sentinel to detect tools= kwarg usage
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.last_messages = messages
        self.last_tools = tools
        self.call_count += 1
        if self.error:
            raise self.error
        yield StreamEvent(kind="done", content=self.content)


def _cfg(**kw):
    base = dict(
        context_window=1_000_000,
        tier1_threshold=0.6,
        tier2_threshold=0.8,
        tier3_threshold=0.95,
        protect_zone_tokens=8_192,
        snip_head_lines=5,
        snip_tail_lines=1,
    )
    base.update(kw)
    return ContextConfig(**base)


# ============================================================
# find_protect_boundary (6 tests)
# ============================================================

def test_find_protect_boundary_empty():
    _, _, find_protect_boundary, *_ = _import_context()
    assert find_protect_boundary([], FakeCounter(), 100) == 0


def test_find_protect_boundary_only_system():
    _, _, find_protect_boundary, *_ = _import_context()
    msgs = [{"role": "system", "content": "hello"}]
    assert find_protect_boundary(msgs, FakeCounter(), 100) == 0


def test_find_protect_boundary_single_user():
    _, _, find_protect_boundary, *_ = _import_context()
    msgs = [{"role": "user", "content": "hi"}]
    assert find_protect_boundary(msgs, FakeCounter(), 100) == 0


def test_find_protect_boundary_budget_less_than_last_user():
    """Budget too small to walk past the last user → clamp keeps last user protected."""
    _, _, find_protect_boundary, *_ = _import_context()
    msgs = [
        {"role": "tool", "content": "aaaaaaaaaa"},   # idx 0, 10 tokens
        {"role": "user", "content": "hello"},         # idx 1, last user
        {"role": "assistant", "content": "world"},    # idx 2
    ]
    boundary = find_protect_boundary(msgs, FakeCounter(), 2)
    # last user (idx 1) MUST be inside protect zone messages[boundary:]
    assert boundary <= 1
    assert boundary == 1


def test_find_protect_boundary_budget_covers_5():
    """5 equal messages, budget covers last 3 → boundary splits at 2."""
    _, _, find_protect_boundary, *_ = _import_context()
    msgs = [{"role": "user", "content": "aaaa"} for _ in range(5)]  # 5 msgs × 4 tokens
    boundary = find_protect_boundary(msgs, FakeCounter(), 12)  # covers 3 (4×3)
    assert boundary == 2  # messages[2:] = last 3 protected


def test_find_protect_boundary_clamp_to_last_user():
    """Tiny budget with user early in list → boundary clamped down to last_user_idx."""
    _, _, find_protect_boundary, *_ = _import_context()
    msgs = [
        {"role": "user", "content": "hello"},        # idx 0, last user
        {"role": "tool", "content": "x" * 50},       # idx 1
        {"role": "assistant", "content": "yy"},      # idx 2
    ]
    boundary = find_protect_boundary(msgs, FakeCounter(), 1)
    assert boundary == 0  # clamped to last_user_idx=0 → all protect


# ============================================================
# apply_tier1_snip (8 tests)
# ============================================================

def test_apply_tier1_snip_tool_head_tail():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg(snip_head_lines=2, snip_tail_lines=1)
    lines = [f"L{i}" for i in range(10)]
    msgs = [{"role": "tool", "content": "\n".join(lines)}]
    apply_tier1_snip(msgs, 1, cfg)
    result_lines = msgs[0]["content"].splitlines()
    assert result_lines[0] == "L0"
    assert result_lines[1] == "L1"
    assert any("omitted" in ln for ln in result_lines)
    assert result_lines[-1] == "L9"


def test_apply_tier1_snip_user_code_block():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg(snip_head_lines=2, snip_tail_lines=1)
    body = "\n".join([f"line{i}" for i in range(10)])
    msgs = [{"role": "user", "content": f"Here:\n```python\n{body}\n```\nDone."}]
    apply_tier1_snip(msgs, 1, cfg)
    result = msgs[0]["content"]
    assert result.startswith("Here:")
    assert result.endswith("Done.")
    assert "```python" in result
    assert "line0" in result
    assert "line9" in result
    assert any("omitted" in ln for ln in result.splitlines())


def test_apply_tier1_snip_skip_protect_zone():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg(snip_head_lines=2, snip_tail_lines=1)
    lines = [f"L{i}" for i in range(10)]
    msgs = [
        {"role": "tool", "content": "\n".join(lines)},  # idx 0 — in range
        {"role": "tool", "content": "\n".join(lines)},  # idx 1 — protect zone
    ]
    apply_tier1_snip(msgs, 1, cfg)  # protect_until=1 → only idx 0 touched
    assert any("omitted" in ln for ln in msgs[0]["content"].splitlines())
    assert msgs[1]["content"] == "\n".join(lines)  # untouched


def test_apply_tier1_snip_skip_protected_pattern():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg(snip_head_lines=2, snip_tail_lines=1, protected_tool_patterns=["__skill$"])
    lines = [f"L{i}" for i in range(10)]
    msgs = [{"role": "tool", "name": "mcp__acme__skill", "content": "\n".join(lines)}]
    apply_tier1_snip(msgs, 1, cfg)
    assert msgs[0]["content"] == "\n".join(lines)  # protected → untouched


def test_apply_tier1_snip_short_content_noop():
    """Tool content with <= head+tail+1 lines is left alone."""
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg(snip_head_lines=5, snip_tail_lines=1)  # threshold = 7
    msgs = [{"role": "tool", "content": "a\nb\nc"}]  # 3 lines
    apply_tier1_snip(msgs, 1, cfg)
    assert msgs[0]["content"] == "a\nb\nc"


def test_apply_tier1_snip_plain_text_untouched():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg()
    prose = "just some prose, no code block here"
    msgs = [{"role": "user", "content": prose}]
    apply_tier1_snip(msgs, 1, cfg)
    assert msgs[0]["content"] == prose


def test_apply_tier1_snip_no_tool_messages():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg()
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    before = [dict(m) for m in msgs]
    apply_tier1_snip(msgs, 2, cfg)
    assert msgs == before


def test_apply_tier1_snip_does_not_delete_messages():
    _, _, _, apply_tier1_snip, *_ = _import_context()
    cfg = _cfg(snip_head_lines=2, snip_tail_lines=1)
    lines = [f"L{i}" for i in range(10)]
    msgs = [{"role": "tool", "content": "\n".join(lines)}, {"role": "user", "content": "go"}]
    n = len(msgs)
    apply_tier1_snip(msgs, 1, cfg)
    assert len(msgs) == n


# ============================================================
# apply_tier2_prune (8 tests)
# ============================================================

def test_apply_tier2_prune_tool_to_placeholder():
    _, _, _, _, apply_tier2_prune, _, _, _, TIER2_TOOL_PLACEHOLDER = _import_context()
    cfg = _cfg()
    msgs = [{"role": "tool", "content": "some long tool output\n" * 5}]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == TIER2_TOOL_PLACEHOLDER


def test_apply_tier2_prune_assistant_first_sentence():
    _, _, _, _, apply_tier2_prune, *_ = _import_context()
    cfg = _cfg()
    msgs = [{"role": "assistant", "content": "First sentence. Second sentence. Third."}]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == "First sentence. [truncated]"


def test_apply_tier2_prune_no_punctuation_fallback():
    _, _, _, _, apply_tier2_prune, *_ = _import_context()
    cfg = _cfg()
    content = "x" * 250  # no punctuation, > 200 chars
    msgs = [{"role": "assistant", "content": content}]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == "x" * 200 + " [truncated]"


def test_apply_tier2_prune_does_not_delete_tool():
    _, _, _, _, apply_tier2_prune, _, _, _, TIER2_TOOL_PLACEHOLDER = _import_context()
    cfg = _cfg()
    msgs = [{"role": "tool", "content": "output"}, {"role": "user", "content": "go"}]
    n = len(msgs)
    apply_tier2_prune(msgs, 1, cfg)
    assert len(msgs) == n
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["content"] == TIER2_TOOL_PLACEHOLDER


def test_apply_tier2_prune_preserves_tool_calls_field():
    _, _, _, _, apply_tier2_prune, *_ = _import_context()
    cfg = _cfg()
    tcs = [{"id": "c1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}]
    msgs = [{"role": "assistant", "content": "First. Second.", "tool_calls": tcs}]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == "First. [truncated]"
    assert msgs[0]["tool_calls"] == tcs


def test_apply_tier2_prune_skip_protect_zone():
    _, _, _, _, apply_tier2_prune, _, _, _, TIER2_TOOL_PLACEHOLDER = _import_context()
    cfg = _cfg()
    msgs = [
        {"role": "tool", "content": "in-range"},     # idx 0 — touched
        {"role": "tool", "content": "protected"},    # idx 1 — protect zone
    ]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == TIER2_TOOL_PLACEHOLDER
    assert msgs[1]["content"] == "protected"


def test_apply_tier2_prune_skip_protected_pattern():
    _, _, _, _, apply_tier2_prune, *_ = _import_context()
    cfg = _cfg(protected_tool_patterns=["__skill$"])
    msgs = [{"role": "tool", "name": "do__skill", "content": "keep me"}]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == "keep me"


def test_apply_tier2_prune_skip_summary_message():
    """assistant with SUMMARY_MARKER_KEY must be left fully intact (Tier3's own output)."""
    _, _, _, _, apply_tier2_prune, *_ = _import_context()
    cfg = _cfg()
    content = "summary text. more sentences here."
    msgs = [{"role": "assistant", "content": content, SUMMARY_MARKER_KEY: True}]
    apply_tier2_prune(msgs, 1, cfg)
    assert msgs[0]["content"] == content


def test_apply_tier2_prune_user_codeblock_force():
    """Tier2 对 user ```代码块 force 截:head=1/tail=0,绕过 threshold。

    spec 强制 Tier2 对 user 代码块用 force=True head=1/tail=0(只保留首行 +
    省略标记)。即使内容短(2 行)也会被截 —— force 路径 threshold = head+tail = 1,
    比 Tier1 的 head+tail+1 低 1,所以 2 行块在 Tier2 被截而 Tier1 不截。
    """
    _, _, _, _, apply_tier2_prune, *_ = _import_context()
    cfg = _cfg()
    code = "```python\nline1\nline2\nline3\nline4\n```"
    msgs = [{"role": "user", "content": code}]
    apply_tier2_prune(msgs, protect_until=1, config=cfg)
    # force head=1/tail=0:fence 保留 + 首行保留 + 省略标记,line2-4 被截
    result = msgs[0]["content"]
    assert "```python" in result           # fence 结构保留
    assert "line1" in result               # 首行(head=1)保留
    assert "line2" not in result           # 被截
    assert "line4" not in result           # tail=0 → 尾行也不留
    assert "3 lines omitted" in result     # OMITTED_TEMPLATE 省略标记

    # force 绕过 threshold:仅 2 行的代码块在 Tier1(head+tail+1=2 → 不截)
    # 但 Tier2 force(threshold = head+tail = 1 → 截)
    short_msgs = [{"role": "user", "content": "```js\nonly1\nonly2\n```"}]
    apply_tier2_prune(short_msgs, protect_until=1, config=cfg)
    assert "only1" in short_msgs[0]["content"]
    assert "only2" not in short_msgs[0]["content"]
    assert "1 lines omitted" in short_msgs[0]["content"]


# ============================================================
# maybe_compact — non-Tier3 (5 tests)
# ============================================================

@pytest.mark.asyncio
async def test_maybe_compact_disabled():
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(enabled=False)
    original = "x" * 200
    msgs = [{"role": "user", "content": original}]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg)
    assert stats.tier == __import__("cc_harness.context", fromlist=["CompactionTier"]).CompactionTier.NONE
    assert msgs[0]["content"] == original  # unchanged


@pytest.mark.asyncio
async def test_maybe_compact_ratio_below_tier1():
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=1000, tier1_threshold=0.6)
    msgs = [{"role": "user", "content": "small"}]  # 5 tokens / 1000 = 0.005
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.NONE
    assert stats.before_tokens == 5
    assert stats.ratio_before < 0.6


@pytest.mark.asyncio
async def test_maybe_compact_tier1_only():
    """Ratio >= tier1 but after Snip drops below tier2 → SNIP."""
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100, tier1_threshold=0.6, tier2_threshold=0.8,
               protect_zone_tokens=5, snip_head_lines=2, snip_tail_lines=1)
    lines = [f"line{i}" for i in range(30)]
    msgs = [
        {"role": "tool", "content": "\n".join(lines)},  # ~210 tokens, snippable
        {"role": "assistant", "content": "done"},        # 4
        {"role": "user", "content": "go"},               # 2, last user
    ]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.SNIP
    assert any("omitted" in ln for ln in msgs[0]["content"].splitlines())
    assert stats.ratio_after < stats.ratio_before


@pytest.mark.asyncio
async def test_maybe_compact_tier1_then_tier2():
    """Tier1 can't reduce (single-line tool) → Tier2 placeholder kicks in → PRUNE."""
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100, tier1_threshold=0.6, tier2_threshold=0.8,
               tier3_threshold=0.95, protect_zone_tokens=5)
    msgs = [
        {"role": "tool", "content": "x" * 200},  # single line → Tier1 no-op
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "go"},
    ]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.PRUNE
    from cc_harness.context import TIER2_TOOL_PLACEHOLDER
    assert msgs[0]["content"] == TIER2_TOOL_PLACEHOLDER


@pytest.mark.asyncio
async def test_maybe_compact_exception_isolation():
    """Exception inside compact must NOT raise; returns NONE stats with error set."""

    class ExplodingCounter(FakeCounter):
        def categorize(self, messages, tools=None):
            raise RuntimeError("boom")

    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100)
    msgs = [{"role": "user", "content": "x" * 200}]
    stats = await maybe_compact(msgs, None, ExplodingCounter(), cfg)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.NONE
    assert stats.error is not None
    assert "boom" in stats.error
    assert stats.before_snapshot is not None  # 异常路径留快照供 debug(spec 要求)


# ============================================================
# apply_tier3_summarize (8 tests) — Plan3 Task5
# ============================================================

@pytest.mark.asyncio
async def test_apply_tier3_summarize_no_previous_summary():
    """No prev summary -> delta starts after system, new summary inserted at idx 1."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="new summary text")
    msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "recent"},
    ]
    stats = await apply_tier3_summarize(msgs, 3, cfg, llm)
    assert stats.summarized is True
    assert msgs[1].get("role") == "assistant"
    assert msgs[1].get(SUMMARY_MARKER_KEY) is True
    assert msgs[1]["content"] == "new summary text"


@pytest.mark.asyncio
async def test_apply_tier3_summarize_found_previous():
    """Prev summary found -> delta after prev, old summary replaced by new."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="updated summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "old summary", SUMMARY_MARKER_KEY: True},
        {"role": "user", "content": "new message"},
        {"role": "user", "content": "recent"},
    ]
    stats = await apply_tier3_summarize(msgs, 3, cfg, llm)
    assert stats.summarized is True
    assert msgs[1]["content"] == "updated summary"
    assert msgs[1].get(SUMMARY_MARKER_KEY) is True
    # Only one summary message should exist (old replaced)
    summary_count = sum(1 for m in msgs if m.get(SUMMARY_MARKER_KEY))
    assert summary_count == 1


@pytest.mark.asyncio
async def test_apply_tier3_summarize_inserts_after_system():
    """Summary is inserted at idx 1 (right after system at idx 0)."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "user", "content": "recent"},
    ]
    await apply_tier3_summarize(msgs, 2, cfg, llm)
    assert msgs[0]["role"] == "system"
    assert msgs[1].get(SUMMARY_MARKER_KEY) is True
    assert msgs[1]["content"] == "summary"


@pytest.mark.asyncio
async def test_apply_tier3_summarize_incremental_across_two_calls():
    """spec 实施期约束 2: second _find_previous_summary idx == first summary_index."""
    _, _, _, _, _, apply_tier3_summarize, _find_previous_summary, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="incremental summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "recent"},
    ]
    # First call
    stats1 = await apply_tier3_summarize(msgs, 3, cfg, llm)
    assert stats1.summarized is True
    assert stats1.summary_index is not None

    # spec 实施期约束 2: _find_previous_summary returns idx == first summary_index
    prev = _find_previous_summary(msgs)
    assert prev is not None
    assert prev[0] == stats1.summary_index

    # Second call — should find prev and produce incremental summary
    stats2 = await apply_tier3_summarize(msgs, 3, cfg, llm)
    assert stats2.summarized is True
    assert stats2.summary_index == stats1.summary_index


@pytest.mark.asyncio
async def test_apply_tier3_summarize_tools_none_passed_to_llm():
    """llm.chat must be called with tools=None (spec: 严禁调用任何工具)."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg"},
        {"role": "user", "content": "recent"},
    ]
    await apply_tier3_summarize(msgs, 2, cfg, llm)
    assert llm.last_tools is None


@pytest.mark.asyncio
async def test_apply_tier3_summarize_llm_error_returns_error():
    """LLM raises -> stats.error set, summarized=False, no raise."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(error=RuntimeError("LLM unavailable"))
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg"},
        {"role": "user", "content": "recent"},
    ]
    stats = await apply_tier3_summarize(msgs, 2, cfg, llm)
    assert stats.summarized is False
    assert stats.error is not None
    assert "LLM unavailable" in stats.error


@pytest.mark.asyncio
async def test_apply_tier3_summarize_records_summary_index():
    """stats.summary_index matches actual insert position in messages."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg"},
        {"role": "user", "content": "recent"},
    ]
    stats = await apply_tier3_summarize(msgs, 2, cfg, llm)
    assert stats.summary_index == 1
    assert msgs[stats.summary_index].get(SUMMARY_MARKER_KEY) is True


@pytest.mark.asyncio
async def test_apply_tier3_summarize_preserves_user_code_blocks():
    """User ```code blocks``` in delta are preserved verbatim in the LLM prompt."""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="summary")
    code = "```python\ndef foo():\n    return 42\n```"
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": f"Here:\n{code}\nDone."},
        {"role": "user", "content": "recent"},
    ]
    await apply_tier3_summarize(msgs, 2, cfg, llm)
    # The user prompt sent to LLM should contain the code block verbatim
    user_prompt = llm.last_messages[1]["content"]
    assert "def foo()" in user_prompt
    assert "```python" in user_prompt


@pytest.mark.asyncio
async def test_apply_tier3_summarize_truncates_large_delta():
    """delta 超 summarize_max_output_tokens*4 → 截断到 70% + 前缀标记(spec 236-237)。

    用小 config(summarize_max_output_tokens=10 → cap=40)省构造:
    50 条 user 消息各 200 字符 → FakeCounter(1char=1token)渲染后远超 cap=40。
    断言 LLM 收到的 user prompt 含截断前缀,且 N = 被省略的早期消息数(49)。
    """
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    # summarize_max_output_tokens=10 → cap=40, keep_budget=7
    cfg = _cfg(summarize_max_output_tokens=10)
    llm = FakeLLM(content="summary")
    big_delta = [{"role": "user", "content": "x" * 200}] * 50  # 50 msgs × 200 tokens
    msgs = [{"role": "system", "content": "sys"}] + big_delta + [
        {"role": "user", "content": "recent"},
    ]
    await apply_tier3_summarize(
        msgs, protect_until=len(msgs) - 1, config=cfg, llm=llm, counter=FakeCounter()
    )
    user_prompt = llm.last_messages[1]["content"]
    assert "delta truncated" in user_prompt
    assert "earlier messages omitted" in user_prompt
    # keep_budget=7 → only the most recent delta message fits → 49 omitted
    assert "49 earlier messages omitted" in user_prompt


# ============================================================
# maybe_compact — Tier3 cascade (2 tests) — Plan3 Task5
# ============================================================

@pytest.mark.asyncio
async def test_maybe_compact_full_cascade_tier3():
    """Tier1+Tier2 insufficient -> Tier3 triggers -> SUMMARIZE with LLM summary."""
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100, tier1_threshold=0.1, tier2_threshold=0.2,
               tier3_threshold=0.3, protect_zone_tokens=5)
    llm = FakeLLM(content="cascade summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "content": "x" * 200},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "go"},
    ]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg, llm)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.SUMMARIZE
    assert stats.summarized is True
    assert any(m.get(SUMMARY_MARKER_KEY) for m in msgs)


@pytest.mark.asyncio
async def test_maybe_compact_tier3_llm_error_degrades_gracefully():
    """Full cascade -> Tier3 -> LLM error -> no raise, error captured in stats."""
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100, tier1_threshold=0.1, tier2_threshold=0.2,
               tier3_threshold=0.3, protect_zone_tokens=5)
    llm = FakeLLM(error=RuntimeError("LLM down"))
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "content": "x" * 200},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "go"},
    ]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg, llm)
    assert stats.error is not None
    assert "LLM down" in stats.error


# ============================================================
# Integration (1 test) — Plan3 Task5
# ============================================================

@pytest.mark.asyncio
async def test_compaction_cascade_real_scenario():
    """Integration: mixed messages through full cascade -> summary + protect zone intact."""
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100, tier1_threshold=0.1, tier2_threshold=0.2,
               tier3_threshold=0.3, protect_zone_tokens=10)
    llm = FakeLLM(content="integrated summary of conversation")
    msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "do task X"},
        {"role": "assistant", "content": "sure, working on it", "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "run_cmd", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": "x" * 200},
        {"role": "assistant", "content": "done with X. result is good."},
        {"role": "user", "content": "now do Y"},
    ]
    original_protect_content = msgs[-1]["content"]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg, llm)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.SUMMARIZE
    assert stats.summarized is True
    # Summary exists
    assert any(m.get(SUMMARY_MARKER_KEY) for m in msgs)
    # Protect zone (last user) untouched
    assert msgs[-1]["content"] == original_protect_content
    assert msgs[-1]["role"] == "user"


# --- Finding 3: Tier3 must remove summarized originals ---

@pytest.mark.asyncio
async def test_apply_tier3_summarize_replaces_delta_with_summary():
    """Finding 3 fix: Tier3 生成的 summary 必须原子地替换被摘要的 delta 消息,
    否则 history 单调增长 → 上下文压缩失败。"""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="replacement summary")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg-1"},
        {"role": "assistant", "content": "reply-1"},
        {"role": "user", "content": "msg-2"},
        {"role": "assistant", "content": "reply-2"},
        {"role": "user", "content": "recent-protect"},
    ]
    pre_len = len(msgs)
    pre_count_user = sum(1 for m in msgs if m.get("role") == "user")
    pre_count_assistant = sum(1 for m in msgs if m.get("role") == "assistant")

    await apply_tier3_summarize(msgs, 5, cfg, llm)

    # 1. summary 必须存在
    summary_count = sum(1 for m in msgs if m.get(SUMMARY_MARKER_KEY))
    assert summary_count == 1, "must have exactly one summary"

    # 2. protect zone(末 user)完整保留
    assert msgs[-1]["content"] == "recent-protect"
    assert msgs[-1]["role"] == "user"

    # 3. Finding 3 fix:被摘要的 msg-1/reply-1/msg-2/reply-2 全部从 messages 中消失。
    # 原 delta 是 msgs[1:5] = 4 条;新长度 = pre_len - 4 + 1(summary) = 3。
    assert len(msgs) == 3, (
        f"after Tier3, expected 3 messages (system + summary + protect user), "
        f"got {len(msgs)}; delta messages NOT removed"
    )
    # 4. 没有任何 "msg-1" / "reply-1" / "msg-2" / "reply-2" 残留
    for m in msgs:
        assert "msg-1" not in m.get("content", ""), "delta message not removed"
        assert "reply-1" not in m.get("content", ""), "delta message not removed"
        assert "msg-2" not in m.get("content", ""), "delta message not removed"
        assert "reply-2" not in m.get("content", ""), "delta message not removed"


@pytest.mark.asyncio
async def test_apply_tier3_summarize_incremental_replaces_old_and_delta():
    """Finding 3 fix + incremental summary:second Tier3 pass 必须只保留:
    1 个新 summary + protect zone;原旧 summary + 累积 delta 全部消失。"""
    _, _, _, _, _, apply_tier3_summarize, _, _, _ = _import_context()
    cfg = _cfg()
    llm = FakeLLM(content="v2 summary")
    msgs = [
        {"role": "system", "content": "sys"},
        # 第一次 Tier3 后这里会有 summary
        {"role": "user", "content": "round-1"},
        {"role": "assistant", "content": "answer-1"},
        # 第二次 Tier3 之前新增的 delta
        {"role": "user", "content": "round-2"},
        {"role": "assistant", "content": "answer-2"},
        # protect zone
        {"role": "user", "content": "protect"},
    ]
    # 第一次 Tier3:delta = msgs[1:5] (round-1 / answer-1 / round-2 / answer-2)
    await apply_tier3_summarize(msgs, 5, cfg, llm)
    # 之后:sys + summary + protect = 3
    assert len(msgs) == 3
    assert msgs[1].get(SUMMARY_MARKER_KEY) is True

    # 模拟第二轮历史增长:在 summary 后新增 delta
    msgs.insert(2, {"role": "user", "content": "round-3"})
    msgs.insert(3, {"role": "assistant", "content": "answer-3"})
    # 现在:sys, summary, round-3, answer-3, protect = 5
    assert len(msgs) == 5

    # 第二次 Tier3:delta_start=2(summary idx+1),protect_until=4
    # 期望:旧 summary 被替换,delta (round-3/answer-3) 被删除,只剩 sys + summary + protect
    await apply_tier3_summarize(msgs, 4, cfg, llm)
    assert len(msgs) == 3
    assert msgs[1].get(SUMMARY_MARKER_KEY) is True
    assert msgs[1]["content"] == "v2 summary"
    assert msgs[-1]["content"] == "protect"
    # round-3 / answer-3 都已删
    for m in msgs:
        assert "round-3" not in m.get("content", "")
        assert "answer-3" not in m.get("content", "")


@pytest.mark.asyncio
async def test_maybe_compact_tier3_drops_message_count_and_tokens():
    """Finding 3 fix: maybe_compact Tier3 cascade 跑完,消息数与 token 数必须下降。"""
    from cc_harness.context import CompactionStats
    _, _, _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100, tier1_threshold=0.1, tier2_threshold=0.2,
               tier3_threshold=0.3, protect_zone_tokens=5)
    llm = FakeLLM(content="summary text")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg-1 " * 30},
        {"role": "assistant", "content": "reply-1 " * 30},
        {"role": "user", "content": "msg-2 " * 30},
        {"role": "assistant", "content": "reply-2 " * 30},
        {"role": "user", "content": "protect"},
    ]
    pre_count = len(msgs)
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg, llm)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.SUMMARIZE
    # Finding 3 fix:消息数从 6 降到 3(sys + summary + protect)
    assert len(msgs) == 3, (
        f"after Tier3 maybe_compact, expected 3 messages, got {len(msgs)} "
        f"(started with {pre_count})"
    )
    # stats.after_tokens < stats.before_tokens
    assert stats.after_tokens < stats.before_tokens, (
        f"after_tokens {stats.after_tokens} should be < before_tokens {stats.before_tokens}"
    )


@pytest.mark.asyncio
async def test_context_projection_preserves_originals_and_versions_summary(tmp_path):
    from copy import deepcopy

    from cc_harness.context import ContextProjection
    from cc_harness.tokens import SUMMARY_MARKER_KEY, TokenCounter

    source = [
        {"role": "system", "content": "system"},
        *[
            {"role": "tool", "name": "read", "content": "old line\n" * 30}
            for _ in range(8)
        ],
        {"role": "user", "content": "current requirement"},
    ]
    original = deepcopy(source)
    config = _cfg(
        context_window=200,
        tier1_threshold=0.1,
        tier2_threshold=0.2,
        tier3_threshold=0.3,
        protect_zone_tokens=5,
    )
    projection = ContextProjection(source, artifact_dir=tmp_path)

    stats = await projection.compact(
        source, None, TokenCounter(), config, FakeLLM(content="stable summary")
    )

    assert source == original
    assert stats.summarized is True
    assert stats.summary_version == 1
    summary = next(m for m in projection.messages if m.get(SUMMARY_MARKER_KEY))
    assert summary["_compaction_source_count"] == len(source)
    assert summary["_compaction_source_digest"].startswith("sha256:")
    assert (tmp_path / "summary-v0001.json").is_file()


@pytest.mark.asyncio
async def test_context_projection_restores_latest_projection_and_increments_version(tmp_path):
    from cc_harness.context import ContextProjection
    from cc_harness.tokens import TokenCounter

    source = [
        {"role": "system", "content": "system"},
        *[{"role": "tool", "name": "read", "content": "old line\n" * 30} for _ in range(8)],
        {"role": "user", "content": "current requirement"},
    ]
    config = _cfg(
        context_window=200,
        tier1_threshold=0.1,
        tier2_threshold=0.2,
        tier3_threshold=0.3,
        protect_zone_tokens=5,
    )
    first = ContextProjection(source, artifact_dir=tmp_path)
    stats1 = await first.compact(
        source, None, TokenCounter(), config, FakeLLM(content="summary one")
    )
    assert stats1.summary_version == 1

    source.append({"role": "assistant", "content": "new delta " * 100})
    source.append({"role": "user", "content": "next requirement"})
    restored = ContextProjection(source, artifact_dir=tmp_path)
    assert restored.summary_version == 1
    assert any(m.get("_compaction_summary") for m in restored.messages)

    stats2 = await restored.compact(
        source, None, TokenCounter(), config, FakeLLM(content="summary two")
    )
    assert stats2.summary_version == 2
    assert (tmp_path / "summary-v0001.json").is_file()
    assert (tmp_path / "summary-v0002.json").is_file()
