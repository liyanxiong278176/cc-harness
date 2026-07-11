"""locomo 评测指标聚合。纯聚合(无 LLM)+ 离线 judge(见 Task 2)。"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict


def compute_by_q_type(results: list[dict]) -> dict:
    """按 q_type 分桶 f1/quality/pass。返回 {q_type: {n, f1_med, quality_med, pass}}。"""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by[r.get("q_type", "unknown")].append(r)
    out: dict[str, dict] = {}
    for qt, rs in by.items():
        f1 = [r["f1"] for r in rs if r.get("f1") is not None]
        q = [r["quality"] for r in rs if r.get("quality") is not None]
        out[qt] = {
            "n": len(rs),
            "f1_med": st.median(f1) if f1 else None,
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


def compute_context_utilization(results: list[dict], context_window: int = 1_000_000) -> dict:
    """利用率 = prompt_tokens / context_window。"""
    pts = [r.get("prompt_tokens", 0) for r in results]
    if not pts:
        return {"avg": 0.0, "peak": 0.0}
    return {
        "avg": st.mean(pts) / context_window,
        "peak": max(pts) / context_window,
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
