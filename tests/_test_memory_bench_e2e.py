"""E2E test for the memory benchmark using real LLM + real embedding API.

NOT auto-collected by pytest (leading underscore). Run manually:
    .venv/Scripts/python.exe -m pytest tests/_test_memory_bench_e2e.py -v
"""
import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("OPENAI_API_KEY") and os.getenv("EMBEDDING_API_KEY")
         and os.getenv("EMBEDDING_BASE_URL") and os.getenv("EMBEDDING_MODEL")),
    reason="需要 OPENAI_* + EMBEDDING_* 环境变量",
)


async def test_memory_bench_e2e(tmp_path):
    from cc_harness.config import load_config
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.decider import LLMDecider
    from cc_harness.memory.store import MemoryStore
    from eval.datasets.memory_bench import load_memory_bench
    from eval.runners.memory_session_runner import run_memory_bench

    cfg = load_config(env_path=pathlib.Path(".env"),
                      mcp_json_path=pathlib.Path("mcp.json"))
    llm = LLMClient(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url,
                    model=cfg.openai_model)
    mcp = MCPClient(cfg.mcp_servers)

    db_path = tmp_path / "e2e.db"
    store = MemoryStore(db_path, embedding_dim=cfg.memory.embedding_dim)
    await store.init_schema()
    embedder = EmbeddingClient(
        base_url=cfg.memory.embedding_base_url,
        api_key=cfg.memory.embedding_api_key,
        model=cfg.memory.embedding_model,
        dim=cfg.memory.embedding_dim,
    )
    decider = LLMDecider(llm)
    service = MemoryService(store, embedder, decider)
    bench = load_memory_bench()

    report = await run_memory_bench(
        bench=bench, llm=llm, mcp=mcp, memory_service=service,
        memory_db_path=db_path, out_dir=tmp_path / "out",
    )
    # Sanity assertions (no LLM-quality assertions; just structural)
    assert report.total_tasks == 17
    assert 0.0 <= report.accuracy <= 1.0
    assert 0.0 <= report.cross_session_recall_precision <= 1.0
    assert 0.0 <= report.cross_session_recall_recall <= 1.0
    assert 0.0 <= report.save_coverage <= 1.0
    assert 0.0 <= report.memory_pollution_rate <= 1.0
    assert 0.0 <= report.long_term_accuracy <= 1.0
    # The output dir was populated
    assert (tmp_path / "out" / "report.json").exists()
    assert (tmp_path / "out" / "trace.jsonl").exists()

    await store.close()
    await embedder.aclose()
