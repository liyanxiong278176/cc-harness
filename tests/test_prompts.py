"""Tests for the Section / PromptComposer infrastructure.

Covers: dataclass defaults, mode filtering, has_tools filtering, priority
ordering, placeholder substitution, unknown-mode and unknown-condition
error paths, the populated SECTION_POOL, and the build_system_prompt()
contract (cwd substitution, all 12 legacy rules still semantically present).
"""
import pytest

from cc_harness.prompts import (
    Section,
    PromptComposer,
    SECTION_POOL,
    build_system_prompt,
)


# --- Section dataclass ---

def test_section_defaults():
    s = Section(name="x", body="y")
    assert s.name == "x"
    assert s.body == "y"
    assert s.priority == 50
    assert s.conditions == ()


def test_section_is_frozen():
    s = Section(name="x", body="y")
    with pytest.raises(Exception):  # FrozenInstanceError
        s.name = "z"


# --- PromptComposer basic ---

# Note: every composer test below passes ctx={"cwd": "/x"} so that the
# `cwd` section in SECTION_POOL can render without a KeyError. The tests
# themselves are about composer behavior, not pool content.

_CTX = {"cwd": "/x"}


def test_composer_no_extras_renders_pool():
    """With the populated SECTION_POOL, a composer with no extras still
    renders the pool's content (the cwd section is the most basic)."""
    out = PromptComposer(ctx=_CTX).render()
    assert "/x" in out
    assert "cc-harness" in out


def test_composer_extra_section_renders_with_placeholder():
    s = Section(name="hi", body="Hello, {name}!", priority=1)
    out = PromptComposer(ctx={**_CTX, "name": "World"}, extra=[s]).render()
    # The extra section appears alongside pool content; we only check the
    # extra is rendered with substitution.
    assert "Hello, World!" in out


def test_composer_joins_sections_with_double_newline():
    a = Section(name="a", body="AAA", priority=1)
    b = Section(name="b", body="BBB", priority=2)
    out = PromptComposer(ctx=_CTX, extra=[a, b]).render()
    assert "AAA\n\nBBB" in out


def test_composer_sorts_by_priority_low_first():
    high = Section(name="hi", body="HIGH", priority=999)
    low = Section(name="lo", body="LOW", priority=1)
    out = PromptComposer(ctx=_CTX, extra=[high, low]).render()
    # Priority 1 comes first; find positions to assert order, not exact
    # match (pool content also interleaves).
    assert out.index("LOW") < out.index("HIGH")


def test_composer_missing_placeholder_raises_keyerror():
    s = Section(name="x", body="cwd={cwd}", priority=1)
    with pytest.raises(KeyError):
        PromptComposer(extra=[s]).render()  # no cwd in ctx


# --- Mode validation ---

def test_composer_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown mode"):
        PromptComposer(mode="bogus", ctx=_CTX)


def test_composer_known_modes_accepted():
    for m in ("coding", "plan", "design"):
        PromptComposer(mode=m, ctx=_CTX)  # should not raise


# --- Conditions ---

def test_mode_condition_excludes_other_modes():
    s = Section(name="plan_only", body="PLAN-ONLY", conditions=("mode==plan",))
    coding = PromptComposer(mode="coding", ctx=_CTX, extra=[s]).render()
    plan = PromptComposer(mode="plan", ctx=_CTX, extra=[s]).render()
    design = PromptComposer(mode="design", ctx=_CTX, extra=[s]).render()
    assert "PLAN-ONLY" not in coding
    assert "PLAN-ONLY" in plan
    assert "PLAN-ONLY" not in design


def test_mode_condition_matches_specific_mode():
    s = Section(name="design_only", body="DESIGN-ONLY", conditions=("mode==design",))
    assert "DESIGN-ONLY" in PromptComposer(mode="design", ctx=_CTX, extra=[s]).render()
    assert "DESIGN-ONLY" not in PromptComposer(mode="coding", ctx=_CTX, extra=[s]).render()


def test_has_tools_condition_filters_by_context():
    s = Section(name="with_tools", body="WITH-TOOLS-MARKER", conditions=("has_tools",))
    no_tools = PromptComposer(ctx=_CTX, extra=[s]).render()
    empty_tools = PromptComposer(ctx={**_CTX, "tools": []}, extra=[s]).render()
    has_tools = PromptComposer(ctx={**_CTX, "tools": [{"name": "shell"}]}, extra=[s]).render()
    assert "WITH-TOOLS-MARKER" not in no_tools
    assert "WITH-TOOLS-MARKER" not in empty_tools
    assert "WITH-TOOLS-MARKER" in has_tools


def test_always_condition_includes_regardless():
    s = Section(name="a", body="ALWAYS-MARKER", conditions=("always",))
    for m in ("coding", "plan", "design"):
        assert "ALWAYS-MARKER" in PromptComposer(mode=m, ctx=_CTX, extra=[s]).render()


def test_unknown_condition_raises():
    s = Section(name="x", body="x", conditions=("bogus==x",))
    with pytest.raises(ValueError, match="unknown condition"):
        PromptComposer(ctx=_CTX, extra=[s]).render()


def test_multiple_conditions_all_must_match():
    """A section with two conditions is only included if BOTH pass."""
    s = Section(
        name="x",
        body="BOTH-MARKER",
        conditions=("mode==plan", "has_tools"),
    )
    # mode=plan + no tools -> excluded
    assert "BOTH-MARKER" not in PromptComposer(mode="plan", ctx=_CTX, extra=[s]).render()
    # mode=coding + has tools -> excluded
    assert "BOTH-MARKER" not in PromptComposer(
        mode="coding", ctx={**_CTX, "tools": [{"name": "x"}]}, extra=[s]
    ).render()
    # mode=plan + has tools -> included
    assert "BOTH-MARKER" in PromptComposer(
        mode="plan", ctx={**_CTX, "tools": [{"name": "x"}]}, extra=[s]
    ).render()


# --- Pool integration ---

def test_section_pool_is_module_level_dict():
    """SECTION_POOL is the shared registry; entries added there are visible
    to all composers."""
    assert isinstance(SECTION_POOL, dict)
    # Snapshot the current state so we can restore it after the test.
    original = dict(SECTION_POOL)
    try:
        SECTION_POOL["test_only"] = Section(name="test_only", body="POOL-OK-MARKER", priority=5)
        assert "POOL-OK-MARKER" in PromptComposer(ctx=_CTX).render()
    finally:
        SECTION_POOL.clear()
        SECTION_POOL.update(original)


# --- build_system_prompt output contract ---

def test_build_system_prompt_substitutes_cwd():
    out = build_system_prompt("/test/cwd")
    assert "/test/cwd" in out


def test_build_system_prompt_signature_accepts_mode():
    """build_system_prompt takes cwd (positional) and mode (keyword, default 'coding')."""
    import inspect
    sig = inspect.signature(build_system_prompt)
    params = list(sig.parameters)
    assert params == ["cwd", "mode"]
    assert sig.parameters["mode"].default == "coding"


def test_build_system_prompt_plan_mode_includes_override():
    out = build_system_prompt("/x", mode="plan")
    assert "Plan 模式" in out
    assert "禁止调用任何工具" in out
    assert "## 目标" in out  # plan format hint
    # Coding-specific sections should NOT appear in plan mode
    # (TODO block and tool discipline are coding-only)
    assert "📝 TODO" not in out
    assert "工具使用纪律" not in out


def test_build_system_prompt_design_mode_includes_override():
    out = build_system_prompt("/x", mode="design")
    assert "Design 模式" in out
    assert "禁止调用任何工具" in out
    assert "mermaid" in out
    assert "变体" in out
    assert "**Tweaks**" in out
    # Same exclusions as plan mode
    assert "📝 TODO" not in out
    assert "工具使用纪律" not in out


def test_build_system_prompt_coding_mode_excludes_plan_and_design_overrides():
    out = build_system_prompt("/x", mode="coding")
    assert "Plan 模式" not in out
    assert "Design 模式" not in out
    # Coding sections ARE present
    assert "📝 TODO" in out
    assert "工具使用纪律" in out


def test_composed_prompt_preserves_all_12_legacy_rules():
    """Semantic equivalence: every concept from the original 12 rules must
    still appear in the composed output. Catches accidental drops during
    the section migration (#3b)."""
    out = build_system_prompt("/x")
    must_contain = [
        # rule 1 + 4 + 11: format markers (思考, 行动, 观察, 结果, Action)
        "思考", "行动", "观察", "结果", "Action:",
        # rule 2: 1-2 sentence thinking before tool use
        "1-2 句",
        # rule 3: TODO block
        "📝 TODO",
        # rule 5: don't force tool calls
        "不要硬塞工具调用",
        # rule 6: don't retry same failed call
        "不要重复同样的失败调用",
        # rule 7: dangerous ops need confirmation
        "rm -rf", "删库", "format",
        # rule 8: no fabrication
        "不要编造文件内容",
        # rule 9: conciseness
        "简洁",
        # rule 10: progress notes for long tasks
        "10 步",
        # rule 12: tool honesty
        "工具能力诚实", "合适的工具",
        # identity + cwd injection
        "MCP", "{cwd}",  # {cwd} is already substituted, so check the format actually worked
    ]
    must_contain = [s for s in must_contain if s != "{cwd}"]  # already substituted
    for needle in must_contain:
        assert needle in out, f"missing rule concept: {needle!r}"


def test_instruction_hierarchy_renders_in_all_modes():
    """G1: 始终生效。coding/plan/design 都应含指令层级段。"""
    from cc_harness.prompts import build_system_prompt
    for mode in ("coding", "plan", "design"):
        p = build_system_prompt("/x", mode=mode)
        assert "指令层级与不可信数据" in p, f"mode={mode} 缺指令层级"
        assert "<untrusted>" in p
        assert "<user_input>" in p


def test_instruction_hierarchy_explains_priority():
    from cc_harness.prompts import build_system_prompt
    p = build_system_prompt("/x", mode="coding")
    assert "开发者" in p and "用户输入" in p and "工具返回" in p


def test_composed_prompt_does_not_leak_unresolved_placeholders():
    """After rendering, no stray {placeholders} (other than intended ones
    in section bodies) should remain."""
    out = build_system_prompt("/x")
    # The {cwd} placeholder should be substituted; no other {xxx} should
    # survive the format() call. (We allow 行动/观察/结果 as plain text;
    # the placeholders we care about are alphabetic {names}.)
    import re
    leftovers = re.findall(r"\{[a-z_]+\}", out)
    assert leftovers == [], f"unresolved placeholders: {leftovers}"


def test_dangerous_ops_forbids_workaround():
    """拒绝姿态(spec 2026-07-02 改动1): dangerous_ops 必须教 agent 不绕道。
    红队 eval/bug/9 暴露 agent 说'请确认/你手动 cat'被判 BROKE。"""
    out = build_system_prompt("/x", mode="coding")
    assert "绝不主动建议绕道方案" in out, "dangerous_ops 缺'不绕道'禁令"
    assert "被安全策略拦截" in out, "dangerous_ops 缺'被拒后如实告知'"
    assert "不要建议用户手动执行任何被权限层拒绝" in out, "tool_discipline #3 未强化"


def test_dangerous_ops_forbids_workaround_is_coding_only():
    """dangerous_ops 是 coding-only(plan/design 不调工具,不需要)。"""
    for m in ("plan", "design"):
        assert "绝不主动建议绕道方案" not in build_system_prompt("/x", mode=m), \
            f"mode={m} 不应含 dangerous_ops"
