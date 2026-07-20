"""locomo 评测指标聚合。纯聚合(无 LLM)+ 离线 judge(见 Task 2)。"""
from __future__ import annotations

import inspect
import json
import statistics as st
from collections import defaultdict


# M5-2 judge prompts(集中常量,plan 写后统一复审)
JUDGE_RECALL = (
    '判断 memory(召回记忆)是否覆盖该 gold evidence(同事实 / 同实体即算覆盖)。\n'
    '只返 JSON {"relevant": bool}。'
)
JUDGE_ENTITIES = (
    '从 gold answer 抽取 key entities(人物 / 事件 / 物品 / 数字)。\n'
    '只返 JSON {"entities": [str, ...]}。'
)
JUDGE_GROUP_CONSIST = (
    '同一 entity 的多个 predicted answer 是否互相一致(同事实 / 同对象,允许近义)。\n'
    '只返 JSON {"consistent": bool, "reason": str}。'
)


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

    chunk_usefulness 全空 records 全部 → 返回 'uncomputed' 字符串(spec §2.3)。
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
      ② async fn:无 .chat → 支持两种签名:
         a) `(prompt: str) -> str`:旧形式,``await judge_llm(system + "\\n" + user)``。
         b) `(system: str, user: str) -> str`:M5-2 形式,introspect positional 参数个数决定调用形态。
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if hasattr(judge_llm, "chat"):
        content = ""
        async for ev in judge_llm.chat(messages, tools=None):  # kwarg 是 tools
            if ev.kind == "done":
                content = ev.content or content
        return content
    # async fn form: introspect n_positional params → 2 args vs 1 arg
    try:
        n_pos = sum(
            1 for p in inspect.signature(judge_llm).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
    except (ValueError, TypeError):
        n_pos = 1
    if n_pos >= 2:
        return await judge_llm(system, user)
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


async def compute_recall(results, qas, conversations, judge_llm) -> dict:
    """#1 记忆召回准确率(单 session 证据版本)。

    `conversations`: Dict[sample_id, conv_dict](非 list)。caller 负责构造,
    例如:{r["sample_id"]: conv for conv in conversations_list}。

    仅算 evidence 全部在同一 session 的 QA(metrics-pass);
    跨 session QA 直接排除(不计入 n_eligible)。

    Returns:
        {n_eligible, n_total_recall, precision, recall}。
        judge_llm is None → 返字符串 'uncomputed'。
    """
    if judge_llm is None:
        return "uncomputed"

    def _evidence_to_text(conv: dict, idx: dict, ev: str) -> str:
        """Resolve LoCoMo evidence ref to utterance text.

        Walks the conversation to find the matching utterance. Handles both:
          - real data: dia_id is the same string as ev ("D1:1")
          - synthetic: dia_id is int (1), speaker="D1", ev="D1:1" → match by speaker+int
        """
        session = idx.get(ev)
        if not session:
            return ""
        # Try direct dia_id match (real data)
        for utt in conv.get(session, []):
            if utt.get("dia_id") == ev:
                return utt.get("text", "")
        # Synthetic: split "D1:1" → speaker="D1", n="1"
        if ":" in ev:
            speaker, _, dia_str = ev.partition(":")
            try:
                dia_int = int(dia_str)
            except ValueError:
                return ""
            for utt in conv.get(session, []):
                if utt.get("speaker") == speaker and utt.get("dia_id") == dia_int:
                    return utt.get("text", "")
        return ""

    from eval.locomo.dataset import build_session_index  # local import 防循环

    p_num, p_den, r_num, r_den = 0, 0, 0, 0
    n_eligible = 0
    n_total_recall = 0

    for r, qa in zip(results, qas):
        evidence = qa.get("evidence") or []
        if not evidence:
            continue
        # Dict 查找(plan 修订:list + sample_id 嵌套查找 bug-prone,改 Dict 接口)
        sample_id = r.get("sample_id") or ""
        conv = conversations.get(sample_id) if isinstance(conversations, dict) else None
        if conv is None:
            continue
        idx = build_session_index(conv)
        sessions = set()
        for ev in evidence:
            s = idx.get(ev)
            if s is None:
                sessions = None  # marker: at least one evidence didn't resolve
                break
            sessions.add(s)
        if sessions is None:
            continue
        if len(sessions) != 1:
            continue
        n_eligible += 1

        recall_calls = [tc for tc in (r.get("tool_calls") or [])
                        if tc.get("name") == "memory_recall"]
        if not recall_calls:
            continue
        n_total_recall += len(recall_calls)
        recall_text = "\n".join(tc.get("result", "") for tc in recall_calls)

        for ev in evidence:
            try:
                text = _evidence_to_text(conv, idx, ev) or ev
                resp = await _judge(judge_llm, JUDGE_RECALL,
                                    f"记忆:\n{recall_text}\n\n证据:\n{text}")
                parsed = json.loads(resp)
                r_den += 1
                if parsed.get("relevant"):
                    r_num += 1
                    p_num += 1
            except Exception:
                pass
        p_den += len(recall_calls)

    return {
        "n_eligible": n_eligible,
        "n_total_recall": n_total_recall,
        "precision": min(1.0, p_num / p_den) if p_den else None,
        "recall":    (r_num / r_den) if r_den else None,
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


def _sample_key(record) -> str:
    return record.get("sample_id", "")


async def compute_consistency(results: list[dict], judge_llm) -> dict:
    """#5 多轮一致性:同 conversation 同 entity ≥2 records → judge 一致性。

    Steps:
      1) by_sample = groupby(results, sample_id)
      2) per record:JUDGE_ENTITIES → [entity1, entity2, ...]
      3) (sample, entity_lower) group:收录该 sample 该 entity 命中的所有 records
      4) 仅保留 len(records) >= 2 的 group
      5) per group:JUDGE_GROUP_CONSIST → consistent bool
      6) drift_rate = inconsistent_groups / total_groups

    Per-record / per-group fail-soft(judge 异常 → skip)。

    judge_llm is None → return string ``"uncomputed"``。
    """
    if judge_llm is None:
        return "uncomputed"

    # 1) by sample
    by_sample: dict[str, list[dict]] = {}
    for r in results:
        by_sample.setdefault(_sample_key(r), []).append(r)

    entity_groups: dict[tuple[str, str], list[dict]] = {}

    for sample_id, recs in by_sample.items():
        for r in recs:
            try:
                resp = await _judge(judge_llm, JUDGE_ENTITIES,
                                    f"gold: {r.get('gold','')}\nquestion: {r.get('question','')}")
                ents = json.loads(resp).get("entities", []) or []
            except Exception:
                continue
            for ent in ents:
                if not isinstance(ent, str) or not ent.strip():
                    continue
                entity_groups.setdefault((sample_id, ent.strip().lower()), []).append(r)

    # 4) 仅保留 ≥2 records 的 group
    eligible = {k: v for k, v in entity_groups.items() if len(v) >= 2}

    n_groups = len(eligible)
    n_drift = 0
    by_sample_drift: dict[str, dict] = {}
    for (sample_id, ent), recs in eligible.items():
        preds = [r.get("predicted", "") for r in recs]
        golds = [r.get("gold", "") for r in recs]
        try:
            pred_block = "\n".join(f"- predicted: {p}" for p in preds)
            gold_block = "\n".join(f"- gold: {g}" for g in golds)
            resp = await _judge(judge_llm, JUDGE_GROUP_CONSIST,
                                f"entity: {ent}\n{pred_block}\n{gold_block}")
            consistent = bool(json.loads(resp).get("consistent", False))
        except Exception:
            continue
        if not consistent:
            n_drift += 1
        bs = by_sample_drift.setdefault(sample_id, {"sample_id": sample_id, "n_groups": 0, "drift_groups": 0})
        bs["n_groups"] += 1
        if not consistent:
            bs["drift_groups"] += 1

    by_sample_rows = []
    for sample_id, bs in by_sample_drift.items():
        ng = bs["n_groups"]
        bs["drift_rate"] = (bs["drift_groups"] / ng) if ng else 0.0
        by_sample_rows.append(bs)

    return {
        "n_groups": n_groups,
        "drift_rate": (n_drift / n_groups) if n_groups else 0.0,
        "by_sample": by_sample_rows,
    }


async def run_judge(
    results: list[dict],
    qas: list,
    conversations: list,
    judge_llm,
    cache_path,
    dataset_sha: str,
) -> dict:
    """5-key aggregate spec §3.4。"""
    has_chunks = any(r.get("chunk_usefulness") for r in results)
    out = {
        "1_recall":      "uncomputed",
        "2_timeliness":  compute_timeliness(results),
        "3_utilization": compute_utilization(results) if has_chunks else "uncomputed",
        "4_compaction":  compute_compaction_v2(results),
        "5_consistency": "uncomputed",
    }
    if judge_llm is None:
        return out

    cache_file = (cache_path / f"locomo-judge-{dataset_sha}.json") if cache_path else None
    if cache_file is not None and cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        return {**out, **cached}

    judged = {
        "1_recall":      await compute_recall(results, qas, conversations, judge_llm),
        "5_consistency": await compute_consistency(results, judge_llm),
    }
    if cache_file is not None:
        cache_file.write_text(
            json.dumps(judged, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return {**out, **judged}
