"""E5 T2.1/T2.2 — drift_detector 注入测试。

T2.1:MemoryService.save + MemoryRetriever.search 注入 drift_detector。
T2.2:repl.run_repl 接受 drift_detector 形参 + finally _drain。

Source of truth:
- T2.1: docs/superpowers/plans/2026-07-21-e5-drift-detection.md lines 1258-1417.
- T2.2: 同 plan lines 1421-1501(已修复 plan 偏差:pytest fixture `monkeypatch` + 简化 mock)。

边界:
- T2.1 纯测试,无 product code 改动。验证:
  - MemoryService.save() 调 drift_detector.check_after_write (ADD 路径)
  - MemoryRetriever.search() 调 drift_detector.check_after_read (top-K 召出)
- T2.2 纯测试,验证:
  - run_repl 接受 drift_detector 关键字形参(无 → TypeError)
  - finally 块 await drift_detector._drain(timeout_s=5.0)
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


# --- T2.2: repl.run_repl 接受 drift_detector 形参 + finally _drain ---


def _fake_read_user_raises_eof():
    """构造一个 _read_user:第一次被 await 就 raise EOFError,触发 repl 主循环
    break 走 finally 块(repl 显式 catch EOFError/KeyboardInterrupt 走 shutdown)。
    """
    async def _fn(prompt: str) -> str:
        raise EOFError
    return _fn


@pytest.mark.asyncio
async def test_repl_passes_drift_detector_to_memory_service(
    tmp_path, monkeypatch,
):
    """repl 接受 drift_detector 形参 + finally 块 await drift_detector._drain。

    沿 E2 reflection_engine 模式(repl.py line 462-468):
    - keyword-only 形参
    - finally 块 try/except 包裹 _drain(timeout_s=5.0)

    Plan 偏差修复(避免回归 / 简化 mock):
    1. 用 pytest fixture `monkeypatch`(自动 cleanup),不直接调 pytest.MonkeyPatch()
    2. fake_llm/fake_mcp 用 MagicMock,只满足形参;mcp.list_tools() 默认 MagicMock
       (repl line 207 `n_tools = len(mcp.list_tools())` → 0 即可)
    3. _read_user 一次性 raise EOFError,让主循环 break 走 finally — 不需要
       构造 memory_extras / manifest / scheduler / reflection_engine 等复杂 boot
    4. 不构造 DriftDetector(本 task 只测形参 + finally 接口;构造留给 T2.3 main.py)
    """
    from cc_harness import repl

    fake_drift = MagicMock()
    fake_drift._drain = AsyncMock()

    monkeypatch.setattr(repl, "_read_user", _fake_read_user_raises_eof())
    # 截短 setup:不需要 init_session_executor/shutdown_session_executor 真副作用,
    # 桩成 no-op 让 finally 走得快
    monkeypatch.setattr(repl, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl, "shutdown_session_executor",
                        AsyncMock())  # repl 调 `await shutdown_session_executor()`

    fake_llm = MagicMock()
    fake_mcp = MagicMock()
    fake_mcp.list_tools = MagicMock(return_value=[])

    await repl.run_repl(
        llm=fake_llm, mcp=fake_mcp, cwd=str(tmp_path),
        drift_detector=fake_drift,
    )

    # finally 块:drift_detector 非 None → await _drain(timeout_s=5.0) 被调
    fake_drift._drain.assert_awaited_once()
    call_kwargs = fake_drift._drain.await_args.kwargs
    assert call_kwargs.get("timeout_s") == 5.0


@pytest.mark.asyncio
async def test_repl_without_drift_detector_skips_drain(tmp_path, monkeypatch):
    """repl.run_repl 不传 drift_detector(默认 None)→ finally 不 await 任何 _drain。

    验证默认 None 守卫 + 向后兼容(无 drift 形参时走老路径,不报错)。
    """
    from cc_harness import repl

    monkeypatch.setattr(repl, "_read_user", _fake_read_user_raises_eof())
    monkeypatch.setattr(repl, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl, "shutdown_session_executor", AsyncMock())

    fake_llm = MagicMock()
    fake_mcp = MagicMock()
    fake_mcp.list_tools = MagicMock(return_value=[])

    # drift_detector 不传(默认 None)— 不应抛 TypeError
    await repl.run_repl(
        llm=fake_llm, mcp=fake_mcp, cwd=str(tmp_path),
    )


def test_main_boot_imports_drift_detector():
    """main.py 可 import，且 DriftDetector API 已接入 run_repl。"""
    import inspect

    import main  # noqa: F401
    from cc_harness.drift.detector import DriftDetector
    from cc_harness.repl import run_repl

    sig = inspect.signature(run_repl)
    assert "drift_detector" in sig.parameters

    methods = [
        "check_after_write",
        "check_after_read",
        "_judge_entities",
        "_judge_group_consistency",
        "_audit",
    ]
    for method in methods:
        assert hasattr(DriftDetector, method), f"DriftDetector 缺 {method}"
