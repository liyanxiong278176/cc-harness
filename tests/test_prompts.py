"""Tests for the Section / PromptComposer infrastructure.

Covers: builder/condition contract, mode filtering, ctx-key condition
gating, the populated SECTION_POOL, and the build_system_prompt()
contract (cwd substitution, all 12 legacy rules still semantically present).
"""
import pytest

from cc_harness.prompts import (
    PromptComposer,
    SECTION_POOL,
    build_system_prompt,
)


def _pool_by_name(name: str):
    """Look up a SECTION_POOL entry by its (name, builder, condition) tuple."""
    for entry in SECTION_POOL:
        if entry[0] == name:
            return entry
    raise KeyError(name)


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
    def hi(ctx):
        return "Hello, {name}!".format(**ctx)
    out = PromptComposer(ctx={**_CTX, "name": "World"}, extra=[hi]).render()
    # The extra section appears alongside pool content; we only check the
    # extra is rendered with substitution.
    assert "Hello, World!" in out


def test_composer_joins_sections_with_double_newline():
    def a(ctx):
        return "AAA"
    def b(ctx):
        return "BBB"
    out = PromptComposer(ctx=_CTX, extra=[a, b]).render()
    assert "AAA\n\nBBB" in out


def test_composer_sorts_extras_in_given_order():
    """Extras are appended in registration order; no implicit priority sort."""
    def high(ctx):
        return "HIGH"
    def low(ctx):
        return "LOW"
    out = PromptComposer(ctx=_CTX, extra=[high, low]).render()
    # Provided order: HIGH then LOW.
    assert out.index("HIGH") < out.index("LOW")


# --- Mode validation ---

def test_composer_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown mode"):
        PromptComposer(mode="bogus", ctx=_CTX)


def test_composer_known_modes_accepted():
    for m in ("coding", "plan", "design", "chat"):
        PromptComposer(mode=m, ctx=_CTX)  # should not raise


# --- Conditions (ctx-key gating) ---

def test_mode_condition_excludes_other_modes():
    # SECTION_POOL['plan_mode_override'] uses condition 'mode_plan'
    # which is set only when mode=='plan'. Verify by composing under
    # different modes.
    coding = PromptComposer(mode="coding", ctx=_CTX).render()
    plan = PromptComposer(mode="plan", ctx=_CTX).render()
    design = PromptComposer(mode="design", ctx=_CTX).render()
    assert "PLAN-ONLY" not in coding  # not in any pool body
    assert "Plan 模式" in plan
    assert "Design 模式" in design


def test_mode_condition_matches_specific_mode():
    # design_mode_override only renders in design mode
    design = PromptComposer(mode="design", ctx=_CTX).render()
    coding = PromptComposer(mode="coding", ctx=_CTX).render()
    assert "mermaid" in design
    assert "mermaid" not in coding


def test_has_tools_condition_filters_by_context():
    # E1/T2.1: condition = ctx-key string. Build a one-off pool that
    # uses a tools-key condition, then verify gating: a condition-gated
    # section only renders when ctx[condition] is not None.
    test_pool = [("with_tools", lambda ctx: "WITH-TOOLS-MARKER", "tools")]
    from cc_harness import prompts as _prompts
    orig = _prompts.SECTION_POOL
    _prompts.SECTION_POOL = test_pool + list(orig)
    try:
        no_tools = PromptComposer(ctx=_CTX).render()              # tools absent
        empty = PromptComposer(ctx={**_CTX, "tools": []}).render() # tools=[]
        has = PromptComposer(ctx={**_CTX, "tools": [{"name": "shell"}]}).render()
    finally:
        _prompts.SECTION_POOL = orig
    # 'is not None' gate: absent -> excluded, present (even empty list) -> included.
    assert "WITH-TOOLS-MARKER" not in no_tools
    assert "WITH-TOOLS-MARKER" in empty
    assert "WITH-TOOLS-MARKER" in has


def test_always_condition_includes_regardless():
    # The internal _ALWAYS_KEY sentinel is always set, so always-included
    # sections render in every mode.
    for m in ("coding", "plan", "design", "chat"):
        out = PromptComposer(mode=m, ctx=_CTX).render()
        assert "cc-harness" in out  # identity section
        assert "指令层级" in out     # instruction_hierarchy section


# --- Pool integration ---

def test_section_pool_is_module_level_list_of_tuples():
    """SECTION_POOL is a list of (name, builder, condition) tuples."""
    assert isinstance(SECTION_POOL, list)
    for entry in SECTION_POOL:
        assert isinstance(entry, tuple) and len(entry) == 3
        name, builder, condition = entry
        assert isinstance(name, str)
        assert callable(builder)
        assert isinstance(condition, str)


# --- build_system_prompt output contract ---

def test_build_system_prompt_substitutes_cwd():
    out = build_system_prompt("/test/cwd")
    assert "/test/cwd" in out


def test_build_system_prompt_signature_accepts_mode_and_extra_ctx():
    """build_system_prompt takes cwd (positional) and mode (keyword, default 'coding'),
    plus an extra_ctx keyword-only dict (T2.1)."""
    import inspect
    sig = inspect.signature(build_system_prompt)
    params = list(sig.parameters)
    assert params == ["cwd", "mode", "extra_ctx"]
    assert sig.parameters["mode"].default == "coding"
    assert sig.parameters["mode"].kind == inspect.Parameter.KEYWORD_ONLY or sig.parameters["mode"].default == "coding"
    assert sig.parameters["extra_ctx"].default is None


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
        "MCP",
    ]
    for needle in must_contain:
        assert needle in out, f"missing rule concept: {needle!r}"


def test_instruction_hierarchy_renders_in_all_modes():
    """G1: 始终生效。coding/plan/design/chat 都应含指令层级段。"""
    from cc_harness.prompts import build_system_prompt
    for mode in ("coding", "plan", "design", "chat"):
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
    # survive. (We allow 行动/观察/结果 as plain text; the placeholders we
    # care about are alphabetic {names}.)
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


def test_tool_discipline_warns_shell_redirect_in_sandbox():
    """sandbox 模式 prompt 教 agent:写文件用 fs 工具,别用 shell 重定向(RO 拒)。"""
    from cc_harness.prompts import build_system_prompt
    out = build_system_prompt("/x", mode="coding")
    assert "写文件用文件工具" in out or "别用 shell 重定向" in out, \
        "缺沙箱模式写文件指导"


# --- chat mode (Plan1 Task3) ---

def test_chat_mode_prompt_has_assistant_guidance():
    """chat system prompt 含助手引导 + 自然对话语义。"""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt("/tmp", mode="chat")
    assert "助手" in prompt        # 本地 AI 助手
    assert "自然" in prompt        # 自然语言回答


def test_chat_mode_excludes_coding_sections():
    """chat 不含 todo_block / tool_discipline(编程纪律)。"""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt("/tmp", mode="chat")
    assert "TODO 块" not in prompt
    assert "工具使用纪律" not in prompt


def test_coding_mode_unaffected():
    """coding prompt 不受 chat section 影响。"""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt("/tmp", mode="coding")
    assert "TODO 块" in prompt  # coding 仍有


# --- Tier 3 summary prompts (Plan3 Task3) ---

def test_summary_user_prompt_renders_prev_and_delta():
    from cc_harness.prompts import summary_user_prompt
    s = summary_user_prompt("历史摘要", ["m1 text", "m2 text"])
    assert "历史摘要" in s and "新增消息" in s

def test_render_messages_preserves_user_codeblock():
    """user 消息 ```代码块原样保留(不修正)。"""
    from cc_harness.prompts import _render_messages_for_summary
    msgs = [{"role": "user", "content": "```python\nx=1\n```"}]
    text = _render_messages_for_summary(msgs)
    assert "```python" in text and "x=1" in text

def test_render_tool_message_prefix():
    from cc_harness.prompts import _render_messages_for_summary
    text = _render_messages_for_summary([{"role": "tool", "content": "result"}])
    assert "[tool result]" in text

def test_render_assistant_toolcall():
    from cc_harness.prompts import _render_messages_for_summary
    text = _render_messages_for_summary([
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}
    ])
    assert "tool_call" in text and "f" in text


# --- Phase 1 Q1 uplift: qa condition + qa_intro section ---

def test_qa_condition_excludes_when_no_qa_category():
    """没设 qa_category → qa_intro 不渲染(向后兼容 non-QA 路径)。"""
    out = PromptComposer(mode="chat", ctx={"cwd": "/x"}).render()
    assert "qa_intro" not in out
    assert "当前问题类型" not in out

def test_qa_condition_includes_when_qa_category_set():
    """设 qa_category → qa_intro 渲染,且模板 {qa_category} 被填。"""
    out = PromptComposer(mode="chat", ctx={"cwd": "/x", "qa_category": 2}).render()
    assert "当前问题类型:QA" in out
    assert "cat=2" in out
    # 必须答规则出现
    assert "必须给出具体答案" in out

def test_qa_condition_works_in_plan_mode():
    """qa condition 与 mode 解耦 — plan + qa 也会渲染(虽然不常见)。"""
    out = PromptComposer(mode="plan", ctx={"cwd": "/x", "qa_category": 5}).render()
    assert "cat=5" in out

def test_qa_intro_section_in_pool_with_qa_condition():
    """SECTION_POOL 注册了 qa_intro + 正确 condition 元数据。"""
    name, builder, condition = _pool_by_name("qa_intro")
    assert condition == "qa_category"
    # Render and check the body mentions must-answer rules
    body = builder({"qa_category": 2})
    assert body is not None
    assert "cat=2" in body

def test_qa_intro_body_mentions_must_answer_rule():
    """qa_intro 段必含"实体名/日期/相关概念换关键词重试" 的硬规则(下游 Phase 2 配合)。"""
    _, builder, _ = _pool_by_name("qa_intro")
    body = builder({"qa_category": 2})
    assert "实体名" in body or "日期" in body
    assert "重试" in body or "换关键词" in body
    # 简洁优先 + 长度匹配
    assert "简洁" in body
    assert "gold" in body

