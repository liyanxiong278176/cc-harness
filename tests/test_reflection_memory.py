"""T3.1 tests: MemoryStore.search_reflections + LLMDecider 扩参 + service 注入.

接口契约(给 T3.2 用):
- MemoryStore.search_reflections(*, limit=5, lookback_h=24.0) -> list[Memory]
- LLMDecider.decide(new_text, similar, *, recent_reflections=None) -> DecisionResult
- MemoryService.save 在 decide 前召 search_reflections 并注入 recent_reflections
"""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_store_search_reflections_filters_by_source(tmp_path):
    """search_reflections 只返 source='reflection' 的 Memory,且按时间倒序。"""
    from cc_harness.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    emb = [0.1, 0.2, 0.3, 0.4]
    # 加 3 条:llm / pipeline / reflection
    await store.add("普通记忆", emb, "llm", session_id="s1")
    await store.add("管线记忆", emb, "pipeline", session_id="s1")
    refl_id = await store.add("反思1", emb, "reflection", session_id="s1")
    # 时间戳错开确保倒序稳定
    await asyncio.sleep(0.01)
    await store.add("反思2", emb, "reflection", session_id="s1")

    out = await store.search_reflections(limit=5, lookback_h=24)
    assert len(out) == 2
    assert all(m.source == "reflection" for m in out)
    assert out[0].id != refl_id  # 倒序:反思2 在前


@pytest.mark.asyncio
async def test_store_search_reflections_respects_lookback(tmp_path):
    """lookback_h 内的才返,外的不返。"""
    from cc_harness.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    emb = [0.1, 0.2, 0.3, 0.4]
    await store.add("老反思", emb, "reflection", session_id="s1")
    # 把 created_at 改到 100h 前
    old = time.time() - 100 * 3600
    await store._db.execute(
        "UPDATE memories SET created_at=? WHERE source='reflection'",
        (old,),
    )
    await store._db.commit()
    out = await store.search_reflections(limit=5, lookback_h=24)
    assert len(out) == 0


@pytest.mark.asyncio
async def test_decider_receives_recent_reflections():
    """LLMDecider.decide(new_text, similar, recent_reflections=...) 注入正确。"""
    from cc_harness.memory.decider import LLMDecider, Decision
    from cc_harness.memory.store import Memory

    captured = {}
    class FakeLLM:
        # 必须真 async generator(yield 直接在 body 内,不是 return gen())—
        # LLMDecider.decide 用 `async for ev in self._llm.chat(...)` 迭代返回值。
        async def chat(self, msgs, tools=None):
            # 验 msgs 内有反思段
            for m in msgs:
                if "你过去 24h 对相似主题的反思" in (m.get("content") or ""):
                    captured["reflection_injected"] = True
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content='{"action": "ADD"}')

    decider = LLMDecider(FakeLLM())
    sim = (Memory(id="m1", text="x", embedding=[0.0], created_at=0,
                  updated_at=0, source="llm"), 0.5)
    recent = [Memory(id="r1", text="反思1", embedding=[0.0], created_at=0,
                     updated_at=0, source="reflection")]
    res = await decider.decide("new", [sim], recent_reflections=recent)
    assert res.action == Decision.ADD
    assert captured.get("reflection_injected") is True


@pytest.mark.asyncio
async def test_decider_no_recent_reflections_works():
    """recent_reflections=None 时走旧路径,prompt 不含反思段。"""
    from cc_harness.memory.decider import LLMDecider, Decision
    from cc_harness.memory.store import Memory

    class FakeLLM:
        async def chat(self, msgs, tools=None):
            for m in msgs:
                assert "你过去 24h" not in (m.get("content") or "")
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content='{"action": "ADD"}')

    decider = LLMDecider(FakeLLM())
    sim = (Memory(id="m1", text="x", embedding=[0.0], created_at=0,
                  updated_at=0, source="llm"), 0.5)
    res = await decider.decide("new", [sim])  # recent_reflections 不传
    assert res.action == Decision.ADD


@pytest.mark.asyncio
async def test_service_save_injects_recent_reflections_to_decider(tmp_path):
    """MemoryService.save 调 decider 前召 search_reflections 并注入。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService

    store = MemoryStore(tmp_path / "mem.db", embedding_dim=4)
    await store.init_schema()
    # 预存 1 条反思
    await store.add("已有反思", [0.1, 0.2, 0.3, 0.4], "reflection", session_id="s1")
    # Fake embedder + decider
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.5, 0.5, 0.5, 0.5])
    decider = MagicMock()
    decider._llm = MagicMock()  # service 用 ._llm 走矛盾检测
    decider.decide = AsyncMock(return_value=MagicMock(action=1))  # ADD
    svc = MemoryService(store=store, embedder=embedder, decider=decider)
    await svc.save("新记忆", source="llm", session_id="s1")
    # 验 decide 收到了 recent_reflections
    call = decider.decide.await_args
    rr = call.kwargs.get("recent_reflections")
    assert rr is not None
    assert len(rr) == 1
    assert rr[0].source == "reflection"
