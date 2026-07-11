"""locomo 评测指标聚合。纯聚合(无 LLM)+ 离线 judge(见 Task 2)。"""
from __future__ import annotations

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
