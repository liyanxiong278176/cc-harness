# Plan 4: 评测指标重建

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重建 locomo 评测报告——从 5 张粗卡(pass/f1/quality/cost/tool-calls)升级到 5 维度细粒度:**q_type 分桶 / 记忆 P·R / 上下文压缩+利用率 / token 时序 / 工具调用准确率**。quality 评委 + tool_calls 数据由 Plan 1 提供,记忆 evidence 由 dataset 提供,压缩数据由 Plan 3 提供。

**Architecture:** ① 新 `eval/locomo/metrics.py`:纯聚合函数(q_type/compaction/utilization/token_series,无 LLM)+ 离线 judge 函数(memory P/R 用 evidence、tool_accuracy,复用 LLMClient);② judge 结果缓存 `locomo-judge-*.json`,无 key 优雅降级;③ `report.py` 改:~10 顶部卡 + q_type 分桶表 + token 时序;④ runner 串联 metrics → report。

**Tech Stack:** Python 3.11 / pytest / cc_harness.llm.LLMClient(judge)/ deepeval(quality,Plan 1)

**关联 spec:** `docs/superpowers/specs/2026-07-10-assistant-chat-memory-compaction-eval-design.md`(子系统④.3-4.5)
**前置:** Plan 1(`results[].tool_calls`/`quality`/chat mode)、Plan 3(`results[].compaction`)
**后续:** 无(Plan 4 收尾,4 plan 齐)

---

## File Structure(Plan 4 涉及)

| 文件 | 责任 | 改动 |
|---|---|---|
| `eval/locomo/metrics.py` | 新 | 5 维度聚合 + 离线 judge |
| `eval/locomo/report.py` | 改 | ~10 卡 + q_type 表 + 时序 |
| `eval/locomo/runner.py` | 改 | 串联 metrics → judge → report |
| `tests/test_metrics.py`(或 `eval/locomo/tests/`) | 新 | 聚合 + judge(mock)单测 |
| `tests/test_report.py`(同 locomo test 目录) | 改/新 | 多卡 + q_type 表渲染 |

> ⚠ **Test 路径决策**:`pyproject` `testpaths=["tests"]` **不收** `eval/locomo/tests/`。实现者先 `ls eval/locomo/tests/` 确认现有 locomo test 位置——若 `test_report.py` 等在 `eval/locomo/tests/`,新 `test_metrics.py` 同放该处、run 命令用 `pytest eval/locomo/tests/`;否则放 root `tests/`、`pytest tests/`。本 plan 命令默认 `pytest tests/`,按确认结果调整。

---

## Task 1: `metrics.py` 纯聚合(q_type / compaction / utilization / token_series)

**Files:**
- Create: `eval/locomo/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: 写失败测试**

`tests/test_metrics.py`:
```python
"""metrics.py 纯聚合单测(无 LLM)。用 fixture results。"""
import pytest

FIXTURE = [  # 3 条 result,2 类 q_type
    {"q_type": "single-hop", "f1": 0.8, "quality": 0.9, "pass": True,
     "prompt_tokens": 50000, "completion_tokens": 100, "cost_usd": 0.01,
     "tool_calls": [{"name": "memory_recall", "args": {"query": "q"}, "ok": True, "result": "找到 1 条"}],
     "compaction": None, "turn_idx": -1, "sample_id": "conv-1"},
    {"q_type": "multi-hop", "f1": 0.2, "quality": 0.3, "pass": False,
     "prompt_tokens": 60000, "completion_tokens": 200, "cost_usd": 0.02,
     "tool_calls": [], "compaction": None, "turn_idx": -1, "sample_id": "conv-1"},
    {"q_type": "single-hop", "f1": 0.6, "quality": None, "pass": False,
     "prompt_tokens": 70000, "completion_tokens": 150, "cost_usd": 0.01,
     "tool_calls": [], "compaction": {"tier": 2, "before": 180000, "after": 150000,
                                      "ratio_before": 0.18, "ratio_after": 0.15},
     "turn_idx": -1, "sample_id": "conv-1"},
]


def test_compute_by_q_type():
    from eval.locomo.metrics import compute_by_q_type
    out = compute_by_q_type(FIXTURE)
    assert "single-hop" in out and "multi-hop" in out
    sh = out["single-hop"]
    assert sh["n"] == 2
    assert sh["pass"] == 1  # 1/2 pass


def test_compute_compaction():
    from eval.locomo.metrics import compute_compaction
    out = compute_compaction(FIXTURE)
    assert out["triggered"] == 1  # 1 条有 compaction tier>0
    assert out["by_tier"][2] == 1  # tier2 一次


def test_compute_context_utilization():
    """利用率 = prompt_tokens / 1M。"""
    from eval.locomo.metrics import compute_context_utilization
    out = compute_context_utilization(FIXTURE, context_window=1_000_000)
    assert out["peak"] == pytest.approx(70000 / 1_000_000)
    assert out["avg"] > 0


def test_compute_token_series():
    from eval.locomo.metrics import compute_token_series
    out = compute_token_series(FIXTURE)
    assert out["prompt"] == [50000, 60000, 70000]
    assert out["cumulative_cost"] == pytest.approx(0.04)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_metrics.py -v`
Expected: FAIL(`metrics.py` 不存在)

- [ ] **Step 3: 实现纯聚合函数**

`eval/locomo/metrics.py`:
```python
"""locomo 评测指标聚合。纯聚合(无 LLM)+ 离线 judge(见 Task 2)。"""
from __future__ import annotations
import statistics as st
from collections import defaultdict


def compute_by_q_type(results: list[dict]) -> dict:
    """按 q_type 分桶 f1/quality/pass。返回 {q_type: {n, f1_med, quality_med, pass}}。"""
    by = defaultdict(list)
    for r in results:
        by[r.get("q_type", "unknown")].append(r)
    out = {}
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
    """压缩指标:triggered 次数、by_tier 分布、平均保留率。"""
    triggered = 0
    by_tier = defaultdict(int)
    retain_ratios = []
    for r in results:
        c = r.get("compaction")
        if c and c.get("tier", 0) > 0:
            triggered += 1
            by_tier[c["tier"]] += 1
            if c.get("before") and c.get("after"):
                retain_ratios.append(c["after"] / c["before"])
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_metrics.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/metrics.py tests/test_metrics.py
git commit -m "feat(locomo-eval): metrics.py 纯聚合(q_type/compaction/utilization/token_series)

4 个无 LLM 聚合函数。Plan4 Task1。"
```

---

## Task 2: `metrics.py` 离线 judge(记忆 P/R + 工具准确率)

**Files:**
- Modify: `eval/locomo/metrics.py`(compute_memory + compute_tool_accuracy)
- Test: `tests/test_metrics.py`
- spec 节:子系统④.4(记忆 P/R 用 evidence)、决策记录 9(judge 离线)

- [ ] **Step 1: 写失败测试(mock LLMClient)**

`tests/test_metrics.py` 加:
```python
async def test_compute_memory_precision_recall():
    """记忆 P@k + R:judge 评 recall 返回记忆 ↔ evidence 相关性。"""
    from eval.locomo.metrics import compute_memory
    # mock judge:返回相关性判断
    async def fake_judge(prompt, **kw):
        return '{"relevant": true}'  # 简化:所有都相关
    results_with_qa = [{
        "q_type": "single-hop", "tool_calls": [
            {"name": "memory_recall", "args": {"query": "q"}, "ok": True,
             "result": "找到 2 条:1. Alice 住北京 2. Bob 是工程师"}],
    }]
    qas = [{"question": "q", "answer": "a", "evidence": ["Alice 住北京"]}]
    out = await compute_memory(results_with_qa, qas, judge_llm=fake_judge)
    assert "precision" in out and "recall" in out
    assert 0.0 <= out["precision"] <= 1.0


def test_compute_tool_accuracy():
    """工具准确率:judge 评每次 tool_call 选择+参数合理性,均值。"""
    from eval.locomo.metrics import compute_tool_accuracy
    async def fake_judge(prompt, **kw):
        return '{"score": 0.8}'
    results = [{"tool_calls": [
        {"name": "memory_recall", "args": {"query": "x"}, "ok": True, "result": "r"}]}]
    import asyncio
    out = asyncio.run(compute_tool_accuracy(results, contexts=["x"], judge_llm=fake_judge))
    assert out["mean"] == pytest.approx(0.8)
    assert out["n"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_metrics.py -k "memory or tool_accuracy" -v`
Expected: FAIL(函数不存在)

- [ ] **Step 3: 实现离线 judge 函数**

`eval/locomo/metrics.py` 加:
```python
import json
import os


async def _judge(judge_llm, system, user) -> str:
    """调 judge LLM,返回文本。
    judge_llm:LLMClient(.chat 返回 **AsyncIterator[StreamEvent]**——流式 async generator,
              不能 `await` 得 str,必须迭代累加)或 async fn(str)->str。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if hasattr(judge_llm, "chat"):
        # ⚠ LLMClient.chat 是 streaming generator:`await judge_llm.chat(...)` 返回 generator 而非 str。
        # 必须迭代 events,取最终 done 事件的 content(参考 agent.py 主循环如何累加 stream)。
        content = ""
        async for ev in judge_llm.chat(messages, tools=None):  # ⚠ kwarg 是 tools 非 tool_specs(LLMClient.chat 签名)
            # 实现者按 StreamEvent 真实结构(llm.py)取 done/content;常见 pattern:
            if getattr(ev, "kind", "") == "done" or getattr(ev, "type", "") == "done":
                content = getattr(ev, "content", None) or content
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
        "precision": (p_num / p_den) if p_den else None,
        "recall": (r_num / r_den) if r_den else None,
    }
    # 注:此为可过 test 的最小实现(R 严格、P 粗估)。实现者可细化 P(逐条记忆判相关 vs 整段)。


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
```

> `compute_memory` 的 evidence 匹配逻辑:对每 QA,取其 memory_recall 的 result(记忆文本),对每条 evidence 调 judge 判"记忆是否覆盖该 evidence"(relevant bool)。P = 相关记忆条数/返回总数;R = 被覆盖 evidence/|evidence|。实现者细化(记忆文本可能多条,需解析 `_format_recall_results` 格式或直接整段判)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_metrics.py -v`
Expected: 全 PASS(含 memory/tool_accuracy mock)

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/metrics.py tests/test_metrics.py
git commit -m "feat(locomo-eval): 离线 judge — 记忆 P/R(evidence)+ 工具准确率

compute_memory P@k/R + compute_tool_accuracy;fail-soft;mock LLM 可测。Plan4 Task2。"
```

---

## Task 3: judge 缓存 + 无 key 优雅降级

**Files:**
- Modify: `eval/locomo/metrics.py`(run_judge 编排:缓存读写 + 无 key 降级)
- Test: `tests/test_metrics.py`

- [ ] **Step 1: 写失败测试**

`tests/test_metrics.py` 加:
```python
def test_run_judge_caches(tmp_path):
    """judge 结果缓存到 json,二次读不重跑 judge。run_judge 是 sync(内部管 event loop,直接调)。"""
    from eval.locomo.metrics import run_judge
    call_count = [0]
    async def counting_judge(s, **kw):
        call_count[0] += 1
        return '{"score": 0.5}'
    cache = tmp_path / "judge.json"
    r1 = run_judge(FIXTURE, [], counting_judge, cache)   # 直接调(sync)
    r2 = run_judge(FIXTURE, [], counting_judge, cache)    # 命中缓存,不重跑
    assert call_count[0] == 1  # FIXTURE 只 1 个 tool_call(record0)→ tool_accuracy 跑 1 次;memory qas=[]→0
    assert r1 == r2


def test_run_judge_no_key_degrades(tmp_path):
    """无 judge_llm(None)→ judge 维度 'uncomputed',纯聚合仍返。"""
    from eval.locomo.metrics import run_judge
    out = run_judge(FIXTURE, [], None, tmp_path / "j.json")  # 直接调(sync)
    assert out["tool_accuracy"] == "uncomputed"
    assert out["by_q_type"]  # 纯聚合仍有
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_metrics.py -k "run_judge" -v`
Expected: FAIL(无 run_judge)

- [ ] **Step 3: 实现 run_judge 编排**

`eval/locomo/metrics.py` 加:
```python
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
            "tool_accuracy": await compute_tool_accuracy(results, [r.get("q_type", "") for r in results], judge_llm),
        }
    judged = asyncio.run(_run())
    if cache_path:
        cache_path.write_text(json.dumps(judged, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**out, **judged}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_metrics.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/metrics.py tests/test_metrics.py
git commit -m "feat(locomo-eval): run_judge 编排 — 缓存 + 无 key 优雅降级

纯聚合总跑;judge 有 llm 才跑 + 缓存 json;无 key 标 uncomputed。Plan4 Task3。"
```

---

## Task 4: `report.py` 多卡 + q_type 分桶表 + 时序

**Files:**
- Modify: `eval/locomo/report.py`(_summary_cards ~10 卡 + 新 _q_type_table + _token_series_block + write_html_report 接 metrics)
- Test: `tests/test_report.py`(新或改)
- spec 节:子系统④.5

- [ ] **Step 1: 写失败测试**

`tests/test_report.py`:
```python
def test_write_html_report_renders_q_type_table(tmp_path):
    """HTML 含 q_type 分桶表 + ~10 卡。"""
    from eval.locomo.report import write_html_report
    results = [...]  # fixture
    metrics = {"by_q_type": {"single-hop": {"n": 2, "f1_med": 0.7, "pass": 1}},
               "utilization": {"peak": 0.07}, "memory": {"precision": 0.6},
               "tool_accuracy": {"mean": 0.8}}
    p = write_html_report(results, tmp_path / "r.html", metrics=metrics)
    html = p.read_text(encoding="utf-8")
    assert "single-hop" in html
    assert "q_type" in html.lower() or "分桶" in html
    assert "0.8" in html  # 工具准确率


def test_write_html_report_uncomputed_judge(tmp_path):
    """judge='uncomputed' → 标'未计算'不崩。"""
    from eval.locomo.report import write_html_report
    metrics = {"memory": "uncomputed", "tool_accuracy": "uncomputed"}
    p = write_html_report([], tmp_path / "r.html", metrics=metrics)
    assert "未计算" in p.read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_report.py -v`
Expected: FAIL(write_html_report 不接 metrics / 无 q_type 表)

- [ ] **Step 3: 改 report.py**

`eval/locomo/report.py`:
1. `write_html_report(results, out_path, metrics=None, title=...)`:metrics 可选(向后兼容);metrics 提供时渲染多卡 + q_type 表
2. `_summary_cards(results, metrics)`:~10 卡(pass/f1-med/quality-med/cost/tool-calls/recall 数/P@k/R/工具准确率/峰值利用率)——从 metrics 取,judge='uncomputed' 显示"未计算"
3. `_q_type_table(by_q_type)`:5 类 × (n/f1-med/quality-med/pass) 表
4. `_token_series_block(token_series)`:简易(前 N 个 prompt token 的 mini 表或 sparkline 文本)
5. 转义/状态色逻辑不变(Task8 修复保留)。**但 `_row` 的 tool_calls 列必须更新**:Plan 1 把 schema 改成 `list[dict]`(`{name,args,ok,result}`),当前 `_row`(`report.py:58`)的 `", ".join(r.get("tool_calls") or [])` 假设 `list[str]` → 会 `TypeError`。改为 `", ".join(tc.get("name","?") for tc in (r.get("tool_calls") or []))`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/report.py tests/test_report.py
git commit -m "feat(locomo-eval): report 多卡(~10)+ q_type 分桶表 + token 时序

write_html_report 接 metrics;judge uncomputed 标'未计算'。Plan4 Task4。"
```

---

## Task 5: runner 串联 metrics → judge → report + 烟测

**Files:**
- Modify: `eval/locomo/runner.py`(main 末尾:metrics.run_judge → write_html_report(metrics=...))
- Test: 集成烟测

- [ ] **Step 1: runner main 串联 metrics**

`eval/locomo/runner.py` `main()` 末尾(`write_html_report` 前),加 metrics 计算:
```python
    # Plan4: 算 metrics(纯聚合 + 离线 judge)
    from eval.locomo.metrics import run_judge
    from eval.locomo.dataset import verify_dataset
    # 取 evidence(需 qa → evidence 映射);无 key 时 judge_llm=None
    judge_llm = llm if (os.getenv("OPENAI_API_KEY") and not args.no_trace) else None
    # 注:runner 里 llm 是 per-sample 构造的;这里用模块级 LLMClient 或复用
    judge_cache = args.output_dir / f"locomo-judge-{ts}.json"
    samples_all = verify_dataset(DEFAULT_FILE)
    # evidence 索引(按 (sample_id, question),避免位置错位):
    evidence_idx = {(s["sample_id"], qa.get("question", "")): qa.get("evidence", [])
                    for s in samples_all for qa in s.get("qa", [])}
    # qas 传 **list**(compute_memory 期望 list[dict]);每个 qa 带 sample_id+question 供查 evidence_idx。
    # 注:result 需有 sample_id+question 才能对齐;Plan 1 runner 若没存 question,需补存或按 sample 内 QA 顺序对齐。
    qas = [{"sample_id": s["sample_id"], **qa} for s in samples_all for qa in s.get("qa", [])]
    metrics = run_judge(all_results, qas, judge_llm, judge_cache)
    write_html_report(all_results, html_path, metrics=metrics)
```

> 实现者细化:`judge_llm` 构造(LLMClient from env)、qa↔result 对齐(按 sample_id+question 或顺序)、`--no-trace` 时不跑 judge。无 key → judge_llm=None → judge 维度 uncomputed。

- [ ] **Step 2: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q && .venv/Scripts/python.exe -m ruff check cc_harness/ tests/ eval/locomo/`
Expected: 全 PASS + lint 干净

- [ ] **Step 3: 端到端烟测(1 样本,全 4 plan 集成)**

Run(用户执行,Plan 1-4 全落地后):
```bash
cd /d/agent_learning/cc-harness
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --output-dir eval/result/locomo-final
```
Expected:
- `[runner] DONE`
- `locomo-final/locomo-judge-*.json` 生成(若 judge_llm=None 则跳过)
- HTML 含:~10 卡 + q_type 分桶表 + token 时序

- [ ] **Step 4: 验证报告 5 维度齐全**

浏览器开 `locomo-final/locomo-report-*.html`,确认:
- q_type 分桶表(5 类,每类 f1/quality/pass)
- 记忆指标卡(recall 数 / P@k / R 或"未计算")
- 压缩卡(triggered / by_tier;1M 下可能 0=真实)
- 利用率卡(avg / peak)
- 工具准确率卡(或"未计算")
- token 时序

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/runner.py
git commit -m "feat(locomo-eval): runner 串联 metrics → judge → report

main 末尾算 5 维度 metrics + 离线 judge(无 key 降级),write_html_report(metrics=)。Plan4 Task5(收尾)。"
```

---

## Plan 4 完成标准

- [ ] Task 1-5 全 commit,`pytest tests/ -x -q` 全绿
- [ ] `ruff check cc_harness/ tests/ eval/locomo/` 干净
- [ ] Task 5 烟测:1 样本跑完,HTML 含 5 维度(q_type/记忆/压缩/利用率/工具准确率)
- [ ] 无 `OPENAI_API_KEY`(judge)时 → judge 维度标"未计算",纯聚合仍报
- [ ] judge 缓存生效(二次跑读 cache 不重跑 judge)

## 4 Plan 全部完成的终态

Plan 1(chat+quality+runner)+ Plan 2(记忆)+ Plan 3(压缩)+ Plan 4(指标)落地后:
- cc-harness = 本地 AI 助手(chat/coding/plan/design 4 mode,默认 coding)
- chat 模式:直接回答 + 全工具 + 记忆 + 压缩
- 生产 REPL chat/coding 接入长期记忆
- run_turn 4-tier 压缩(1M 窗口,所有 mode)
- locomo 评测:5 维度真实指标(quality 出分 / f1 真实 / 记忆 P-R / 压缩 / 利用率 / 工具准确率)
- 定位文案(README/CLAUDE.md)统一改(可放任一 plan 末尾或单独 commit)
