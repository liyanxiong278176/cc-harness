"""Tests for cc_harness/context.py — protect boundary + Tier1 Snip + Tier2 Prune + maybe_compact.

Plan3 Task4: 27 tests (Tier3 Summarize is Task5, not here).
Per spec 2026-06-12 「test_context.py 38 test 分布」.
"""
import json

import pytest

from cc_harness.config import ContextConfig
from cc_harness.tokens import SUMMARY_MARKER_KEY


# Lazy import — context.py created in Step 3; tests collected after.
def _import_context():
    from cc_harness.context import (
        CompactionTier,
        CompactionStats,
        find_protect_boundary,
        apply_tier1_snip,
        apply_tier2_prune,
        maybe_compact,
        TIER2_TOOL_PLACEHOLDER,
    )
    return (CompactionTier, CompactionStats, find_protect_boundary,
            apply_tier1_snip, apply_tier2_prune, maybe_compact, TIER2_TOOL_PLACEHOLDER)


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
    _, _, _, _, apply_tier2_prune, _, TIER2_TOOL_PLACEHOLDER = _import_context()
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
    _, _, _, _, apply_tier2_prune, _, TIER2_TOOL_PLACEHOLDER = _import_context()
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
    _, _, _, _, apply_tier2_prune, _, TIER2_TOOL_PLACEHOLDER = _import_context()
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
    _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(enabled=False)
    original = "x" * 200
    msgs = [{"role": "user", "content": original}]
    stats = await maybe_compact(msgs, None, FakeCounter(), cfg)
    assert stats.tier == __import__("cc_harness.context", fromlist=["CompactionTier"]).CompactionTier.NONE
    assert msgs[0]["content"] == original  # unchanged


@pytest.mark.asyncio
async def test_maybe_compact_ratio_below_tier1():
    _, _, _, _, _, maybe_compact, _ = _import_context()
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
    _, _, _, _, _, maybe_compact, _ = _import_context()
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
    _, _, _, _, _, maybe_compact, _ = _import_context()
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

    _, _, _, _, _, maybe_compact, _ = _import_context()
    cfg = _cfg(context_window=100)
    msgs = [{"role": "user", "content": "x" * 200}]
    stats = await maybe_compact(msgs, None, ExplodingCounter(), cfg)
    from cc_harness.context import CompactionTier
    assert stats.tier == CompactionTier.NONE
    assert stats.error is not None
    assert "boom" in stats.error
    assert stats.before_snapshot is not None  # 异常路径留快照供 debug(spec 要求)
