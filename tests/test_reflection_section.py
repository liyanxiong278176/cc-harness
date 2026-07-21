"""Tests for the reflection section (E2 反思节点 T2.1).

5 cases:
1. SECTION_POOL 包含 "reflection" entry
2. _reflection_section 在 last_neg_reflection=None 或缺时返 None
3. _reflection_section 用 <上一轮反思> XML 标签包裹
4. _reflection_section 在长反思下截断到 ~200 token
5. build_system_prompt(extra_ctx={"last_neg_reflection": ...}) 把反思段拼到末尾
"""
from cc_harness.prompts import (
    SECTION_POOL, build_system_prompt, _reflection_section,
)


def test_section_pool_has_reflection_entry():
    names = [name for name, _, _ in SECTION_POOL]
    assert "reflection" in names


def test_reflection_section_returns_none_when_no_neg():
    assert _reflection_section({"last_neg_reflection": None}) is None
    assert _reflection_section({}) is None


def test_reflection_section_wraps_with_xml_tags():
    out = _reflection_section({"last_neg_reflection": "上次失败了 X"})
    assert out is not None
    assert "<上一轮反思>" in out
    assert "上次失败了 X" in out
    assert "</上一轮反思>" in out


def test_reflection_section_truncates_at_200_tokens():
    """长反思应被截断,~200 token。"""
    long = "字" * 1000
    out = _reflection_section({"last_neg_reflection": long})
    assert out is not None
    # 反射体本身 ≤ 200 字
    assert out.count("字") <= 250  # 留余量


def test_reflection_section_appears_in_build_system_prompt(tmp_path):
    """build_system_prompt 应在末尾拼 reflection section(若 last_neg 非 None)。"""
    # 起一个最小 cwd(tmp_path) → Section pool 跑通
    ctx = {"last_neg_reflection": "上轮 max_iter 触达,反思根因:没用 Grep。"}
    out = build_system_prompt(
        cwd=tmp_path, mode="coding", extra_ctx=ctx,
    )
    assert "上轮反思" in out or "<上一轮反思>" in out
    assert "Grep" in out
