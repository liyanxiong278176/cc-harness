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

    Q3 deps(7 key):service/retriever/pipeline/recall/store/persona_path/scenarios_dir。
    Q4 offload 锭(独立 try,fail-soft):refs_dir/canvas_path/offload closure/canvas
    closure/read_ref_spec + config 字段(threshold/offload_ratio/context_window 等)。
    Q4 init hiccup 不破 Q3 —— offload 段失败仅缺 offload key/extras 不加 read_ref。
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

    extras: list[dict] = [
        {"spec": MEMORY_RECALL_SPEC, "handler": memory_recall_handler, "deps": {"retriever": retriever}},
        {"spec": MEMORY_SAVE_SPEC, "handler": memory_save_handler, "deps": {"service": service}},
    ]
    deps: dict = {
        "service": service, "retriever": retriever, "pipeline": pipeline,
        "recall": _recall, "store": store,
        "persona_path": persona_path, "scenarios_dir": scenarios_dir,
    }

    # --- Q4 Task4:offload 锭(refs/canvas/closures/read_ref tool)。
    # 独立 try —— offload init hiccup(import 失败 / config 异常 / 目录建失败)
    # 不破 Q3 deps,Q3 extras 仍返回;仅缺 offload key + read_ref 不入 extras。
    # llm 复用 Q3 decider_llm(已在上方 try 内构造,此处一定 in-scope 非 None)。 ---
    try:
        from cc_harness.memory.offload.offload import maybe_offload
        from cc_harness.memory.offload.mermaid import update_canvas
        from cc_harness.memory.offload.read_ref import READ_REF_SPEC, read_ref_handler
        from cc_harness.memory.config import load_memory_config
        from cc_harness.config import load_context_config

        # load_memory_config:yaml 缺失 → 默认 MemoryConfig();env 覆盖(MEMORY_OFFLOAD_*)。
        # 与 Q3 把 persona.md / scenarios/ 放 db_path.parent 同源,policy.yaml 也从那找。
        mem_cfg = load_memory_config(db_path.parent / "policy.yaml")
        # load_context_config 读 CONTEXT_WINDOW env(locomo smoke 用 32768 降窗口测压缩);
        # 用裸 ContextConfig() 会硬编码 1M → T7 offload_ratio 算错 + smoke 下 Q4 不触发。
        ctx_cfg = load_context_config()

        # refs/canvas 落 <db_path.parent>/memory/(与 db_path 同根,持久化跨轮)
        refs_dir = db_path.parent / "memory" / "refs"
        canvas_path = db_path.parent / "memory" / "canvas.md"
        refs_dir.mkdir(parents=True, exist_ok=True)
        canvas_path.parent.mkdir(parents=True, exist_ok=True)

        # 闭包烘焙:把 refs_dir/canvas_path/decider_llm/mem_cfg 钉进闭包,
        # 对外暴露简洁签名(T5 agent hook 调)。镜像 Q3 _recall 闭包范式。
        # threshold 默认 = mem_cfg.offload_threshold,caller 可 override。
        async def _offload(
            result_text, tool_name, args, *,
            threshold=mem_cfg.offload_threshold, token_counter,
        ):
            return await maybe_offload(
                result_text, tool_name, args, threshold,
                refs_dir=refs_dir, llm=decider_llm, token_counter=token_counter,
            )

        async def _canvas(node_id, label, summary, edge_from):
            return await update_canvas(
                node_id, label, summary, edge_from,
                canvas_path=canvas_path, llm=decider_llm,
            )

        deps.update(
            refs_dir=refs_dir,
            canvas_path=canvas_path,
            offload=_offload,
            canvas=_canvas,
            read_ref_spec=READ_REF_SPEC,
            enabled=mem_cfg.offload_enabled,
            canvas_inject=mem_cfg.offload_canvas_inject,
            threshold=mem_cfg.offload_threshold,
            mermaid_max_token_ratio=mem_cfg.mermaid_max_token_ratio,
            offload_ratio=mem_cfg.offload_ratio,
            context_window=ctx_cfg.context_window,
        )
        extras.append({
            "spec": READ_REF_SPEC, "handler": read_ref_handler,
            "deps": {"refs_dir": refs_dir},
        })
    except Exception as e:
        print(f"[memory] offload init failed: {e}; running without offload tools")

    return extras, deps
