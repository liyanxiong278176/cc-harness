"""Locomo eval runner — replays 10 long conversations, scores QA, outputs HTML + langfuse trace.

Usage:
    python eval/locomo/runner.py                          # full 10 samples
    python eval/locomo/runner.py --limit 1 --no-trace   # smoke
    python eval/locomo/runner.py --no-memory-tools       # baseline (no memory)
    python eval/locomo/runner.py --resume                 # from .checkpoint.json
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from dotenv import dotenv_values  # 已在 pyproject.toml(整个 cc-harness 都用,本 spec 不改)
from cc_harness.config import ContextConfig  # Plan3: 压缩配置
from eval.locomo import dataset as ds
from eval.locomo.evaluator import evaluate_qa
from eval.locomo.report import write_html_report
from eval.locomo.trace import LocomoTrace
from eval.locomo.download_dataset import verify_dataset, DEFAULT_FILE

CHECKPOINT = REPO / "eval/locomo/.checkpoint.json"
POLICY_LOCAL = REPO / "eval/locomo/policy_local.yaml"


def _env():
    e = {**os.environ, **{k: v for k, v in dotenv_values(REPO / ".env").items() if v}}
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _load_policy():
    """Read policy_local.yaml (locomo subsystem). Default to all-allowed."""
    if not POLICY_LOCAL.exists():
        return {"enabled": True, "trace_to_langfuse": True, "max_turns_per_sample": 500,
                "sample_timeout_s": 1800, "inject_memory_tools": True,
                "clear_memory_tags": ["locomo/"]}
    import yaml
    return (yaml.safe_load(POLICY_LOCAL.read_text(encoding="utf-8")) or {}).get("locomo_eval", {})


def _make_initial_messages(turn_text: str, speaker: str) -> list[dict]:
    """Convert locomo turn to initial messages list."""
    return [{"role": "user", "content": f"[{speaker}] {turn_text}"}]


async def _build_memory_extras(policy: dict):
    """locomo runner 的 memory extras 构造。复用共享 helper(build_memory_extras)。

    inject_memory_tools gate 留在此处(locomo kill-switch);
    db=logs/locomo_memory.db(eval 隔离,与生产 logs/memory.db 分开)。
    失败优雅降级由 helper 负责(返 ([], None))。
    """
    if not policy.get("inject_memory_tools", True):
        return [], None
    from cc_harness.memory.extras import build_memory_extras
    return await build_memory_extras(_env(), REPO / "logs" / "locomo_memory.db")


async def _clear_memory_tags(tags: list[str]):
    """Delete memories matching tag patterns (locomo isolation). Async, since MemoryStore is async."""
    if not tags:
        return
    try:
        from cc_harness.memory.store import MemoryStore
        from cc_harness.memory.embedding import EmbeddingClient
        from cc_harness.memory.decider import LLMDecider
        from cc_harness.memory.service import MemoryService
        from cc_harness.llm import LLMClient
    except ImportError:
        print("[runner] memory not available; skip tag clear")
        return
    try:
        env = _env()
        emb_base = env.get("EMBEDDING_BASE_URL") or env["OPENAI_BASE_URL"]
        emb_key = env.get("EMBEDDING_API_KEY") or env["OPENAI_API_KEY"]
        emb_model = env.get("EMBEDDING_MODEL", "BAAI/bge-m3")
        emb_dim = int(env.get("EMBEDDING_DIM", "1024"))

        store = MemoryStore(db_path=REPO / "logs" / "locomo_memory.db", embedding_dim=emb_dim)
        await store.init_schema()
        embedder = EmbeddingClient(
            base_url=emb_base, api_key=emb_key, model=emb_model, dim=emb_dim, timeout_s=10.0,
        )
        decider_llm = LLMClient(
            api_key=env["OPENAI_API_KEY"], model=env["OPENAI_MODEL"], base_url=env["OPENAI_BASE_URL"],
        )
        decider = LLMDecider(llm=decider_llm)
        service = MemoryService(store=store, embedder=embedder, decider=decider)
        for tag in tags:
            try:
                n = await service.delete_by_tag(tag)
                print(f"[runner] cleared {n} memories with tag '{tag}'")
            except Exception as e:
                print(f"[runner] clear tag '{tag}' failed: {e}")
    except Exception as e:
        print(f"[runner] clear_memory_tags failed: {e}")


async def _run_sample(sample: dict, policy: dict, extras: list[dict], trace: LocomoTrace) -> list[dict]:
    """Replay a single sample. Returns list of per-QA result dicts."""
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.agent import run_turn

    parsed = ds.parse_sample(sample)
    turns = list(ds.iter_turns(parsed))[: policy.get("max_turns_per_sample", 500)]
    started = time.time()
    sample_timeout_s = policy.get("sample_timeout_s", 1800)

    # Construct LLM + MCP
    env = _env()
    llm = LLMClient(
        api_key=env["OPENAI_API_KEY"],
        model=env["OPENAI_MODEL"],
        base_url=env["OPENAI_BASE_URL"],
    )
    mcp = MCPClient({})  # locomo 不需要 MCP 工具(只用 extra_native_specs)
    await mcp.start()

    try:
        messages: list[dict] = []  # 累积全对话;run_turn mutate in place
        for turn_idx, turn in enumerate(turns):
            if time.time() - started > sample_timeout_s:
                return [{"sample_id": parsed.sample_id, "turn_idx": -1, "q_type": "n/a",
                         "status": "timeout", "f1": None, "quality": None, "pass": False,
                         "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                         "tool_calls": []}]
            span = trace.start_turn(turn_idx, turn.text)
            # 追加新 turn 到累积 messages(不覆盖)
            messages.append({"role": "user", "content": f"[{turn.speaker}] {turn.text}"})
            try:
                stats = await run_turn(
                    messages, llm, mcp,
                    extra_native_specs=extras,
                    max_iter=4, mode="chat", cwd=str(REPO),
                    context_config=ContextConfig(),  # Plan3: 长对话触发压缩
                )
            except Exception as e:
                trace.record_tool(span, "agent_crash", {"err": str(e)[:200]}, {"ok": False})
                # agent_crash: sample 剩余 QA 全标 agent_crash
                remaining = list(ds.iter_qa(parsed))
                return [{"sample_id": parsed.sample_id, "turn_idx": -1, "q_type": q.category,
                         "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                         "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                         "tool_calls": []} for q in remaining]

            # Record aggregate LLM usage for this turn
            trace.record_llm(span, env.get("OPENAI_MODEL", "?"),
                             messages, stats, {"prompt_tokens": stats.api_prompt_tokens,
                                               "completion_tokens": stats.api_completion_tokens})

        # Ask each QA(基于累积的 messages,带全对话上下文)
        results = []
        for qa in ds.iter_qa(parsed):
            qa_messages = list(messages) + [{"role": "user", "content": qa.question}]
            span = trace.start_turn(-1, qa.question)
            try:
                stats = await run_turn(
                    qa_messages, llm, mcp,
                    extra_native_specs=extras,
                    max_iter=6, mode="chat", cwd=str(REPO),
                    context_config=ContextConfig(),  # Plan3: QA 上下文触发压缩
                )
                predicted = qa_messages[-1].get("content", "") or ""
                trace.record_llm(span, env.get("OPENAI_MODEL", "?"),
                                 qa_messages, stats, {"prompt_tokens": stats.api_prompt_tokens,
                                                       "completion_tokens": stats.api_completion_tokens})
            except Exception as e:
                results.append({
                    "sample_id": parsed.sample_id, "turn_idx": -1, "q_type": qa.category,
                    "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                    "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                    "tool_calls": [], "error": str(e)[:200],
                })
                continue

            eval_result = evaluate_qa(qa.question, predicted, qa.answer)
            cost_usd = _estimate_cost(stats.api_prompt_tokens, stats.api_completion_tokens)
            results.append({
                "sample_id": parsed.sample_id,
                "turn_idx": -1,
                "q_type": qa.category,
                "status": "ok" if eval_result["quality"] is not None else "quality_null",
                "f1": eval_result["f1"],
                "quality": eval_result["quality"],
                "pass": eval_result["pass"],
                "prompt_tokens": stats.api_prompt_tokens,
                "completion_tokens": stats.api_completion_tokens,
                "cost_usd": cost_usd,
                "tool_calls": stats.tool_call_log,  # Plan1: 从 tool_call_log 取(替代 [] TODO)
                "compaction": _compaction_to_dict(stats.compaction),  # Plan3: 压缩统计(Plan4 消费)
            })
            trace.score("f1", eval_result["f1"])
            if eval_result["quality"] is not None:
                trace.score("quality", eval_result["quality"])
            trace.update(eval_result["trace_payload"])
        return results
    finally:
        await mcp.shutdown()


def _compaction_to_dict(cs) -> dict | None:
    """CompactionStats → dict(Plan4 消费)。None → None。"""
    if cs is None:
        return None
    return {
        "tier": int(cs.tier),
        "before_tokens": cs.before_tokens,
        "after_tokens": cs.after_tokens,
        "ratio_before": cs.ratio_before,
        "ratio_after": cs.ratio_after,
    }


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Rough cost estimate (DeepSeek pricing). Override via env if needed."""
    # DeepSeek v3: $0.14/M in, $0.28/M out (as of 2026-07)
    in_rate = float(os.environ.get("LOCOMO_COST_IN", "0.14")) / 1_000_000
    out_rate = float(os.environ.get("LOCOMO_COST_OUT", "0.28")) / 1_000_000
    return prompt_tokens * in_rate + completion_tokens * out_rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--no-check-trace", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-memory-tools", action="store_true")
    ap.add_argument("--output-dir", type=Path, default=REPO / "eval" / "result")
    args = ap.parse_args()

    # Plan1: 让 memory_save 等 ASK 工具在 batch 模式放行
    # (in-process run_turn → confirm_tool 读 os.getenv → ASK 自动 yes)
    os.environ.setdefault("CC_HARNESS_AUTOCONFIRM", "always")

    policy = _load_policy()
    if not policy.get("enabled", True):
        print("[runner] locomo_eval disabled in policy_local.yaml; exit 0")
        return 0

    try:
        samples = verify_dataset(DEFAULT_FILE)
    except (FileNotFoundError, ValueError) as e:
        print(f"[red]locomo data error: {e}\n[red]Run: python eval/locomo/download_dataset.py")
        return 2

    samples = samples[:args.limit]
    if args.resume and CHECKPOINT.exists():
        done_ids = set(json.loads(CHECKPOINT.read_text(encoding="utf-8")).get("done", []))
        samples = [s for s in samples if s["sample_id"] not in done_ids]
        print(f"[runner] resume: {len(done_ids)} done, {len(samples)} remaining")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")
    html_path = args.output_dir / f"locomo-report-{ts}.html"
    json_path = args.output_dir / f"locomo-results-{ts}.json"

    all_results = []
    done = []
    if json_path.exists() and not args.resume:
        all_results = json.loads(json_path.read_text(encoding="utf-8"))

    # Pre-warm: clear old memory tags (isolation)
    inject_memory = (not args.no_memory_tools) and policy.get("inject_memory_tools", True)
    if inject_memory:
        asyncio.run(_clear_memory_tags(policy.get("clear_memory_tags", ["locomo/"])))

    enabled_trace = (not args.no_trace) and policy.get("trace_to_langfuse", True)
    if not args.no_check_trace and enabled_trace:
        if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
            print("[yellow]LANGFUSE_* env not set; trace will be no-op (graceful)")
            enabled_trace = False

    async def amain():
        nonlocal all_results, done
        extras, mem_deps = await _build_memory_extras(
            {**policy, "inject_memory_tools": inject_memory}
        )

        for sample in samples:
            print(f"[runner] sample {sample['sample_id']} ...", flush=True)
            trace = LocomoTrace(sample["sample_id"], enabled=enabled_trace)
            try:
                results = await _run_sample(sample, policy, extras, trace)
            except Exception as e:
                results = [{"sample_id": sample["sample_id"], "turn_idx": -1, "q_type": "n/a",
                            "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                            "tool_calls": [], "error": str(e)[:200]}]
            all_results.extend(results)
            done.append(sample["sample_id"])
            json_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=1), encoding="utf-8")
            CHECKPOINT.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
            trace.flush()
            n_pass = sum(1 for r in results if r.get("pass"))
            print(f"[runner]   {sample['sample_id']}: {len(results)} qa, {n_pass} pass", flush=True)

    asyncio.run(amain())
    write_html_report(all_results, html_path)
    print(f"[runner] DONE. results: {json_path}  html: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())