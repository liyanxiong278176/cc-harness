"""Shared helper: construct memory tools (memory_recall/save) as extra_native_specs.

Used by both locomo runner (eval) and repl (production). Caller owns the
inject_memory_tools gate (kill-switch) and db_path (isolation).
"""
from __future__ import annotations
from pathlib import Path


async def build_memory_extras(env: dict, db_path: Path) -> tuple[list[dict], dict | None]:
    """Return (extras, deps). extras: [{spec, handler, deps}].

    async because MemoryStore.init_schema() is async (store.py:44).
    Any dependency failure (missing EMBEDDING_*, sqlite-vec missing, schema init
    failure) → graceful degrade: print warning, return ([], None).
    """
    try:
        from cc_harness.memory.store import MemoryStore
        from cc_harness.memory.embedding import EmbeddingClient
        from cc_harness.memory.decider import LLMDecider
        from cc_harness.memory.retriever import MemoryRetriever
        from cc_harness.memory.service import MemoryService
        from cc_harness.memory.tools import (
            MEMORY_RECALL_SPEC, MEMORY_SAVE_SPEC,
            memory_recall_handler, memory_save_handler,
        )
        from cc_harness.memory.pipeline import MemoryPipeline
        from cc_harness.memory.recall import layered_recall
        from cc_harness.llm import LLMClient
    except ImportError as e:
        print(f"[memory] import failed: {e}; running without memory tools")
        return [], None
    try:
        emb_base = env.get("EMBEDDING_BASE_URL") or env["OPENAI_BASE_URL"]
        emb_key = env.get("EMBEDDING_API_KEY") or env["OPENAI_API_KEY"]
        emb_model = env.get("EMBEDDING_MODEL", "BAAI/bge-m3")
        emb_dim = int(env.get("EMBEDDING_DIM", "1024"))

        store = MemoryStore(db_path=db_path, embedding_dim=emb_dim)
        await store.init_schema()
        embedder = EmbeddingClient(
            base_url=emb_base, api_key=emb_key, model=emb_model, dim=emb_dim, timeout_s=10.0,
        )
        decider_llm = LLMClient(
            api_key=env["OPENAI_API_KEY"], model=env["OPENAI_MODEL"], base_url=env["OPENAI_BASE_URL"],
        )
        decider = LLMDecider(llm=decider_llm)
        service = MemoryService(store=store, embedder=embedder, decider=decider)
        retriever = MemoryRetriever(store=store, embedder=embedder)
    except Exception as e:
        print(f"[memory] service init failed: {e}; running without memory tools")
        return [], None

    # --- Q3 Task7: pipeline + 分层 recall callable(closure 绑定 retriever /
    # persona_path / scenarios_dir)。persona_path/scenarios_dir 必须先赋值
    # 再定义 _recall,否则 closure 引用会 NameError。 ---
    pipeline = MemoryPipeline(llm=decider_llm, service=service)
    persona_path = db_path.parent / "persona.md"
    scenarios_dir = db_path.parent / "scenarios"

    async def _recall(q, **kw):
        return await layered_recall(
            retriever, persona_path, scenarios_dir, q,
            top_k=kw.get("top_k", 5), timeout_s=kw.get("timeout_s", 5.0),
        )

    extras = [
        {"spec": MEMORY_RECALL_SPEC, "handler": memory_recall_handler, "deps": {"retriever": retriever}},
        {"spec": MEMORY_SAVE_SPEC, "handler": memory_save_handler, "deps": {"service": service}},
    ]
    return extras, {
        "service": service, "retriever": retriever, "pipeline": pipeline,
        "recall": _recall, "store": store,
        "persona_path": persona_path, "scenarios_dir": scenarios_dir,
    }
