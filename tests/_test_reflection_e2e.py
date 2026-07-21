"""E2E: 真 LLM 端到端跑 reflection_node。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_reflection_e2e.py -v

需要 OPENAI_API_KEY / EMBEDDING_* 等 env 才跑。
"""
from __future__ import annotations
import os
import pytest


@pytest.mark.asyncio
async def test_e2e_max_iter_triggers_reflection(tmp_path):
    """真 LLM: 触发 max_iter → 真反思 → 真写 memory → 真召出。"""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; E2E gated")
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set; E2E gated")

    # 占位: 实施员在此组装完整 E2E:
    # 1. 构造 memory_service + reflection_engine
    # 2. 调 agent.run_turn(max_iter=3, reflection_engine=engine) 触发 max_iter
    # 3. engine._drain 等后台反思
    # 4. 断言 engine._last_neg is not None
    # 5. 断言 store.search_reflections(limit=5, lookback_h=24) 至少 1 条
    pytest.skip("E2E 占位 — 实施员补(T3.3 task 留作 post-merge ticket)")
