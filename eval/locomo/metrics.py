"""locomo 评测指标聚合。纯聚合(无 LLM)+ 离线 judge(见 Task 2)。"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict


def compute_by_q_type(results: list[dict]) -> dict:
    """按 q_type 分桶 f1/semantic_f1/quality/pass。返回 {q_type: {n, f1_med, semantic_f1_med, quality_med, pass}}。"""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by[r.get("q_type", "unknown")].append(r)
    out: dict[str, dict] = {}
    for qt, rs in by.items():
        f1 = [r["f1"] for r in rs if r.get("f1") is not None]
        sem = [r["semantic_f1"] for r in rs if r.get("semantic_f1") is not None]
        q = [r["quality"] for r in rs if r.get("quality") is not None]
        out[qt] = {
            "n": len(rs),
            "f1_med": st.median(f1) if f1 else None,
            "semantic_f1_med": st.median(sem) if sem else None,
            "quality_med": st.median(q) if q else None,
            "pass": sum(1 for r in rs if r.get("pass")),
        }
    return out


def compute_compaction(results: list[dict]) -> dict:
    """压缩指标:triggered 次数、by_tier 分布、平均保留率(after_tokens/before_tokens)。

    读长 key ``before_tokens``/``after_tokens``(对齐 runner._compaction_to_dict 实际输出)。
    """
    triggered = 0
    by_tier: dict[int, int] = defaultdict(int)
    retain_ratios: list[float] = []
    for r in results:
        c = r.get("compaction")
        if c and c.get("tier", 0) > 0:
            triggered += 1
            by_tier[c["tier"]] += 1
            before = c.get("before_tokens")
            after = c.get("after_tokens")
            if before and after:
                retain_ratios.append(after / before)
    return {
        "triggered": triggered,
        "by_tier": dict(by_tier),
        "avg_retain": st.mean(retain_ratios) if retain_ratios else None,
    }


def compute_compaction_v2(results: list[dict]) -> dict:
    """#4 上下文压缩率:per-tier 分桶 + 整体 avg_retain。

    tier=0 表示该 record 未触发压缩(compaction is None 或 tier=0);
    tier>=1 表示压缩过。
    """
    by_tier: dict[int, dict] = {t: {"tier": t, "trigger_n": 0, "pass": 0,
                                     "retain_sum": 0.0, "retain_count": 0}
                                  for t in (0, 1, 2, 3)}
    total_compressed = 0
    for r in results:
        c = r.get("compaction")
        tier = 0 if c is None else int(c.get("tier", 0))
        by_tier[tier]["trigger_n"] += 1
        if r.get("pass"):
            by_tier[tier]["pass"] += 1
        if tier >= 1:
            total_compressed += 1
        before = c.get("before_tokens") if c else None
        after = c.get("after_tokens") if c else None
        if before and after and before > 0:
            ratio = after / before
            by_tier[tier]["retain_sum"] += ratio
            by_tier[tier]["retain_count"] += 1
    by_tier_rows = []
    compressed_tier_avgs: list[float] = []
    for t in (0, 1, 2, 3):
        row = by_tier[t]
        n = row["trigger_n"]
        avg_retain = (row["retain_sum"] / row["retain_count"]) if row["retain_count"] else None
        if t >= 1 and avg_retain is not None:
            compressed_tier_avgs.append(avg_retain)
        by_tier_rows.append({
            "tier": t,
            "trigger_n": n,
            "avg_retain": avg_retain,
            "pass_rate": (row["pass"] / n) if n else None,
        })
    return {
        "by_tier": by_tier_rows,
        "total_compressed_n": total_compressed,
        "overall_avg_retain": st.mean(compressed_tier_avgs) if compressed_tier_avgs else None,
    }


def compute_timeliness(results: list[dict]) -> dict:
    """#2 时效性:category=3(Temporal)子集的 pass_rate + 中位数。纯聚合。"""
    subset = [r for r in results if str(r.get("q_type")) == "3"]
    n = len(subset)
    if n == 0:
        return {"n": 0, "pass_rate": None, "f1_med": None, "semantic_f1_med": None}
    pass_rate = sum(1 for r in subset if r.get("pass")) / n
    f1_vals = [r["f1"] for r in subset if r.get("f1") is not None]
    sem_vals = [r["semantic_f1"] for r in subset if r.get("semantic_f1") is not None]
    return {
        "n": n,
        "pass_rate": pass_rate,
        "f1_med": st.median(f1_vals) if f1_vals else None,
        "semantic_f1_med": st.median(sem_vals) if sem_vals else None,
    }


def compute_context_utilization(results: list[dict], context_window: int = 1_000_000) -> dict:
    """利用率 = prompt_tokens / context_window。"""
    pts = [r.get("prompt_tokens", 0) for r in results]
    if not pts:
        return {"avg": 0.0, "peak": 0.0}
    return {
        "avg": st.mean(pts) / context_window,
        "peak": max(pts) / context_window,
    }


def compute_utilization(results: list[dict]) -> dict | str:
    """#3 上下文利用率:weighted useful token / prompt_token,纯聚合。

    chunk_usefulness 全空 records 全部 → 返回 'uncomputed' 字符串(spec §3.5)。
    """
    ratios = []
    for r in results:
        chunks = r.get("chunk_usefulness") or []
        if not chunks:
            continue
        weighted = sum(c.get("tokens", 0) * c.get("useful_score", 0) for c in chunks)
        prompt = r.get("prompt_tokens") or 0
        if prompt > 0:
            ratios.append(weighted / prompt)
    if not ratios:
        return "uncomputed"
    ratios_sorted = sorted(ratios)
    return {
        "n": len(ratios),
        "avg": st.mean(ratios),
        "p50": st.median(ratios_sorted),
        "p90": ratios_sorted[max(0, int(len(ratios_sorted) * 0.9) - 1)],
        "min": ratios_sorted[0],
        "max": ratios_sorted[-1],
    }


def compute_token_series(results: list[dict]) -> dict:
    """逐 record prompt token + 累计 cost(results 顺序)。"""
    return {
        "prompt": [r.get("prompt_tokens", 0) for r in results],
        "completion": [r.get("completion_tokens", 0) for r in results],
        "cumulative_cost": sum(r.get("cost_usd", 0) for r in results),
    }


async def _judge(judge_llm, system, user) -> str:
    """调 judge LLM,返回文本。

    judge_llm 两种形态:
      ① LLMClient:有 ``.chat(messages, tools=None)`` → AsyncIterator[StreamEvent]。
         必须 ``async for ev in llm.chat(...)`` 迭代,取 done 事件的 ev.content(str)。
      ② async fn(str)->str:无 .chat → ``await judge_llm(system + "\\n" + user)``。
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if hasattr(judge_llm, "chat"):
        content = ""
        async for ev in judge_llm.chat(messages, tools=None):  # kwarg 是 tools
            if ev.kind == "done":
                content = ev.content or content
        return content
    return await judge_llm(system + "\n" + user)


async def compute_memory(results, qas, judge_llm) -> dict:
    """记忆 P@k + R。judge 评 recall 返回记忆 ↔ gold evidence 相关性。
    R = evidence 被覆盖比例;P = 返回记忆中相关比例(粗估)。fail-soft(judge 挂→跳过)。"""
    p_num, p_den, r_num, r_den = 0, 0, 0, 0
    for r, qa in zip(results, qas):
        recall_calls = [tc for tc in (r.get("tool_calls") or [])
                        if tc.get("name") == "memory_recall"]
        evidence = qa.get("evidence") or []
        if not recall_calls or not evidence:
            continue
        recall_text = "\n".join(tc.get("result", "") for tc in recall_calls)
        n_mems = max(1, len(recall_calls))   # 估算返回记忆条数(粗)
        for ev in evidence:
            try:
                resp = await _judge(judge_llm,
                    "判断记忆是否覆盖该证据,输出 JSON {relevant: bool}。",
                    f"记忆:\n{recall_text}\n\n证据:\n{ev}")
                if json.loads(resp).get("relevant"):
                    r_num += 1
                    p_num += 1
            except Exception:
                pass
            r_den += 1
        p_den += n_mems
    return {
        "precision": min(1.0, p_num / p_den) if p_den else None,
        "recall": (r_num / r_den) if r_den else None,
    }


async def compute_tool_accuracy(results, contexts, judge_llm) -> dict:
    """工具准确率:judge 评每 tool_call 选择+参数合理性 0-1,均值。"""
    scores = []
    for r, ctx in zip(results, contexts):
        for tc in r.get("tool_calls") or []:
            try:
                resp = await _judge(judge_llm,
                    "评工具调用合理性,输出 JSON {score: 0-1}。",
                    f"语境: {ctx}\n调用: {tc['name']}({tc['args']})")
                d = json.loads(resp)
                scores.append(float(d.get("score", 0)))
            except Exception:
                continue  # fail-soft
    return {"mean": st.mean(scores) if scores else None, "n": len(scores)}


def run_judge(results, qas, judge_llm, cache_path) -> dict:
    """编排:纯聚合(总跑)+ 离线 judge(有 llm 才跑,缓存)。
    无 judge_llm → judge 维度 'uncomputed'。返回汇总 dict。"""
    import asyncio

    out = {
        "by_q_type": compute_by_q_type(results),
        "compaction": compute_compaction(results),
        "utilization": compute_context_utilization(results),
        "token_series": compute_token_series(results),
    }
    if judge_llm is None:
        out["memory"] = out["tool_accuracy"] = "uncomputed"
        return out
    if cache_path and cache_path.exists():
        return {**out, **json.loads(cache_path.read_text(encoding="utf-8"))}

    async def _run():
        return {
            "memory": await compute_memory(results, qas, judge_llm),
            "tool_accuracy": await compute_tool_accuracy(
                results, [r.get("q_type", "") for r in results], judge_llm
            ),
        }

    judged = asyncio.run(_run())
    if cache_path:
        cache_path.write_text(json.dumps(judged, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**out, **judged}
