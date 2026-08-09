"""Test: _clear_memory_tags 必须调 store.close(),否则进程退出时 aiosqlite
后台 worker 线程在主事件循环关闭后调用 call_soon_threadsafe 失败,
打出 Event loop is closed 噪音。

Bug 现状: eval/locomo/runner.py:_clear_memory_tags 创建 MemoryStore + init_schema,
但从未 await store.close(),runner.main 退出后 store._db 仍开着,
aiosqlite worker 线程 → 'Event loop is closed' traceback。
"""
from unittest import mock

import pytest


@pytest.mark.asyncio
async def test_clear_memory_tags_closes_store(monkeypatch):
    """runner._clear_memory_tags 必须 await store.close()。"""
    from eval.locomo import runner

    closed = {"count": 0}

    class FakeStore:
        async def init_schema(self):
            pass
        async def close(self):
            closed["count"] += 1

    # 走 imports 短路,不让真实 LLM/embedding 进来
    monkeypatch.setattr("cc_harness.memory.store.MemoryStore",
                        lambda **kw: FakeStore())
    monkeypatch.setattr("cc_harness.memory.embedding.EmbeddingClient",
                        lambda **kw: mock.MagicMock())
    monkeypatch.setattr("cc_harness.memory.decider.LLMDecider",
                        lambda **kw: mock.MagicMock())
    monkeypatch.setattr("cc_harness.memory.service.MemoryService",
                        lambda **kw: mock.MagicMock(
                            delete_by_tag=mock.AsyncMock(return_value=0)))
    monkeypatch.setattr("cc_harness.llm.LLMClient",
                        lambda **kw: mock.MagicMock())

    await runner._clear_memory_tags(["locomo/test"])

    assert closed["count"] >= 1, (
        f"_clear_memory_tags 应至少 await store.close() 1 次,实际 {closed['count']}"
    )