"""E5 T2.1 — MemoryService.save + MemoryRetriever.search 注入 drift_detector 测试。

Source of truth: docs/superpowers/plans/2026-07-21-e5-drift-detection.md lines 1258-1417.

边界:纯测试,无 product code 改动。验证:
- MemoryService.save() 调 drift_detector.check_after_write (ADD 路径)
- MemoryRetriever.search() 调 drift_detector.check_after_read (top-K 召出)
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from cc_harness.memory.retriever import MemoryRetriever
from cc_harness.memory.service import MemoryService
from cc_harness.memory.store import Memory, MemoryStore


@pytest.fixture
async def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await s.init_schema()
    return s


@pytest.fixture
def fake_drift():
    """Fake DriftDetector。check_after_write / check_after_read 都返 []."""
    det = MagicMock()
    det.check_after_write = AsyncMock(return_value=[])
    det.check_after_read = AsyncMock(return_value=[])
    return det


@pytest.fixture
def fake_decider():
    """Fake LLMDecider(直接返 ADD)。"""
    dec = MagicMock()
    dec.decide = AsyncMock()
    return dec


@pytest.mark.asyncio
async def test_memory_service_save_triggers_drift_check(
    store, fake_drift, fake_decider,
):
    """MemoryService.save 写盘后 → 调 drift_detector.check_after_write。

    fake_decider 让 LLM 决策直接返 ADD(跳过 search_reflections);然后 save 走
    store.add → result_action_mem → 矛盾检测(no-op,无 decider._llm)→ drift
    调 check_after_write。
    """
    from cc_harness.memory.decider import Decision, DecisionResult

    # fake decider 返 ADD
    fake_decider.decide = AsyncMock(
        return_value=DecisionResult(action=Decision.ADD)
    )
    # fake embedder:返 4-dim 浮点
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])

    svc = MemoryService(
        store=store, embedder=embedder, decider=fake_decider,
        drift_detector=fake_drift,
    )
    result = await svc.save("hello world", "llm", session_id="s1")
    assert result.action == "ADD"
    # drift 被调
    fake_drift.check_after_write.assert_awaited_once()
    # session_id 传对
    call_kwargs = fake_drift.check_after_write.await_args.kwargs
    assert call_kwargs["session_id"] == "s1"
    # similar 是 list[Memory](search_similar 返 list[(Memory, dist)] → 需 unpack)
    assert isinstance(call_kwargs["similar"], list)
    # turn_idx 是 int
    assert isinstance(call_kwargs["turn_idx"], int)
    # new_memory 是 Memory 实例
    assert isinstance(call_kwargs["new_memory"], Memory)


@pytest.mark.asyncio
async def test_memory_retriever_search_triggers_drift_check(store, fake_drift):
    """MemoryRetriever.search 召出后 → 调 drift_detector.check_after_read。

    先 store.add 2 条同 entity → search 召出 → recall weighter → drift check。
    """
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])

    # 先存 2 条 memory 让 search 召出
    await store.add("Carol 1985", [0.5, 0.5, 0.5, 0.5], "llm", session_id="s1")
    await store.add("Carol 1990", [0.5, 0.5, 0.5, 0.5], "llm", session_id="s1")

    retr = MemoryRetriever(
        store=store, embedder=embedder, top_k=2, token_budget=200,
        drift_detector=fake_drift,
    )
    weighted = await retr.search("Carol", top_k=2)
    # 召出 ≥1 条
    assert len(weighted) >= 1
    # drift 被调
    fake_drift.check_after_read.assert_awaited_once()
    # results 是 list[Memory](已 unpack tuple)
    call_kwargs = fake_drift.check_after_read.await_args.kwargs
    assert isinstance(call_kwargs["results"], list)
    assert all(isinstance(m, Memory) for m in call_kwargs["results"])
    # session_id 传对("s1" 或 "default" 都行,实测两 memory 都是 s1)
    assert call_kwargs["session_id"] in ("s1", "default")
