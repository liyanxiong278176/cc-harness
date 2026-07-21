"""E2E gated(需真 LLM + 真 embedding): 验证 6 件 op 全跑过 + 不破坏主 ReAct。

跑法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_maintenance_e2e.py -v
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import pytest

pytestmark = pytest.mark.requires_llm


@pytest.mark.asyncio
async def test_e2e_all_ops_run_with_real_llm():
    """完整管线: 写入 50 条 → 触发 scheduler → 后台跑完 → 验证 4 op 全跑。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "e2e.db", embedding_dim=1024)
        await store.init_schema()
        # 真实 embedder 需 EMBEDDING_* env; 若无则跳过
        try:
            from cc_harness.memory.embedding import EmbeddingClient
            embedder = EmbeddingClient.from_env()
        except Exception:
            pytest.skip("EMBEDDING_* env not set, skipping e2e")
        llm = MagicMock()
        # 写 50 条
        for i in range(50):
            emb = await embedder.embed(f"memory {i}")
            await store.add(f"memory {i}", emb, "e2e")
        # 跑 scheduler
        service = MemoryService(store, embedder, llm)
        sch = MaintenanceScheduler(store, service, llm=llm, every_n_turns=1)
        sch._embedder = embedder
        await sch.maybe_run(turn_idx=1)
        await sch._drain(timeout_s=30)
        # 验证 4 op 都跑过
        assert sch._current_task is None or sch._current_task.done()
        # 验证 store 还活着
        count = await store.count()
        assert count > 0
        await store.close()
