"""E5 E2E: 真 LLM 端到端跑 drift detection。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_drift_e2e.py -v
"""
from __future__ import annotations
import os
import pytest


@pytest.mark.asyncio
async def test_e2e_drift_detected_on_real_conversation(tmp_path):
    """真 LLM: 同 entity 多次写入 → drift_rate > 0.5 → emit drift_detected neg。"""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; E2E gated")
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set; E2E gated")

    # 实施员写:
    # 1. 构造 DriftDetector + MemoryService + E2 ReflectionEngine
    # 2. 写 3 条同 entity 不一致(例 "Caroline 1985", "Caroline 1990", "Caroline 1980")
    # 3. check_after_write
    # 4. 断言 emit 至少 1 次 drift_detected severity=neg
    # 5. 断言 store.search_reflections 召出 source='drift' 反思(留 ticket,可能需 E2 改 search_reflections 跨 source)
    pytest.skip("E2E 占位 — 实施员补(T2.4 task 留作 post-merge ticket)")