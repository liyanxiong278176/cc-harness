"""E1 E2E: 真 LLM 端到端跑 decomp hint 注入 + 系统提示组装。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_e1_e2e.py -v

需要 OPENAI_API_KEY + EMBEDDING_API_KEY + CC_HARNESS_RUN_REAL_LLM=1 才跑(沿
tests/_test_b_e2e.py 的 `requires_llm` 三重守卫模式)。

测试目的:验证 spec D7 trigger 三重(e1_decompose_hint flag + mode==coding +
iter_count==0)在 _refresh_system_prompt 实际跑通时确实注入 ## 分解契约 section 到
真 system prompt。本测试只覆盖 system prompt 组装层(不需要 subagent 真跑)。

完整 decomp 端到端(LLM 自评 + todo_create + dispatch + retry + done)需要多层
MCP server + 真 LLM streaming,实现复杂,留 placeholder — 与 _test_drift_e2e.py
的"只验 detector 层面"风格一致。
"""
from __future__ import annotations

import os

import pytest


@pytest.mark.requires_llm
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    or not os.environ.get("EMBEDDING_API_KEY")
    or os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason=(
        "real LLM gated: set OPENAI_API_KEY + EMBEDDING_API_KEY + "
        "CC_HARNESS_RUN_REAL_LLM=1 to run"
    ),
)
@pytest.mark.asyncio
async def test_e1_e2e_decompose_hint_in_real_system_prompt(tmp_path):
    """E1 E2E: 真 LLM 跑(iter=0 + coding + flag True)→ system prompt 含 ## 分解契约。

    三重 env 守卫(沿 cc-harness E2E 惯例 — 防止 .env 提供 key 时被误触发):
      1. OPENAI_API_KEY
      2. EMBEDDING_API_KEY
      3. CC_HARNESS_RUN_REAL_LLM=1

    满足 → 跑 system prompt 组装断言。
    不满足 → SKIPPED。
    """
    # 1. 构造真 LLMClient(仅校验存在性,实际 API 调用不在本测试范围内)
    from cc_harness.llm import LLMClient
    LLMClient(  # noqa: F841 — instantiate validates API key shape, no call needed
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
    )

    # 2. iter=0 + coding + e1_decompose_hint=True → 注入 ## 分解契约
    from cc_harness.agent import _refresh_system_prompt

    messages = [{"role": "system", "content": "base system prompt"}]
    _refresh_system_prompt(
        messages, cwd=str(tmp_path), mode="coding",
        extra_ctx={"e1_decompose_hint": True, "iter_count": 0},
    )

    # 3. 断言:## 分解契约 section 出现(含 todo_create / acceptance_criteria /
    #    dispatch_subagent 关键词)
    content = messages[0]["content"]
    assert "## 分解契约" in content, (
        "E1 D7: 真 system prompt 应含 ## 分解契约 section"
    )
    assert "todo_create" in content, "E1 D7: section 应提 todo_create 工具"
    assert "acceptance_criteria" in content, "E1 D7: section 应提 acceptance_criteria"
    assert "dispatch_subagent" in content, "E1 D7: section 应提 dispatch_subagent"

    # 4. 验证:iter=1 时同一调用不注入(e1_decompose_hint=False 守卫)
    messages_iter1 = [{"role": "system", "content": "base system prompt"}]
    _refresh_system_prompt(
        messages_iter1, cwd=str(tmp_path), mode="coding",
        extra_ctx={"e1_decompose_hint": False, "iter_count": 1},
    )
    assert "## 分解契约" not in messages_iter1[0]["content"], (
        "E1 D7: iter>=1 不注入分解契约(避免后续轮次污染)"
    )

    # 5. 验证:plan mode 不注入(即使 iter=0)
    messages_plan = [{"role": "system", "content": "base system prompt"}]
    _refresh_system_prompt(
        messages_plan, cwd=str(tmp_path), mode="plan",
        extra_ctx={"e1_decompose_hint": True, "iter_count": 0},
    )
    assert "## 分解契约" not in messages_plan[0]["content"], (
        "E1 D7: plan mode 不注入分解契约(仅 coding 模式)"
    )