# Q1 指标公允(semantic f1 + pass 重构)实现 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`).

**Goal:** 给 locomo eval 加 semantic_f1(LLM judge 语义等价 0-1)当主指标,重构 pass(semantic>0.7 主 + token_f1 fail-soft 兜底),直击 pass 39%/f1 med 0.032 根因。token_f1 降辅、quality 降诊断。

**Architecture:** 改 4 文件(无新 module):`evaluator.py` 加 `semantic_f1`(复用 `metrics._judge`)+ `evaluate_qa` 变 async + pass 重构;`metrics.py:compute_by_q_type` 加 semantic_f1_med 列;`report.py` 3 处加列/卡;`runner.py` QA 循环 await + result dict 加字段。

**Tech Stack:** Python 3.11 / pytest(pytest-asyncio auto mode)/ OpenAI 兼容 LLM judge(deepseek,复用 runner `llm`)

**关联 spec:** `docs/superpowers/specs/2026-07-12-q1-metric-fairness-design.md`(`571314a`)
**前置:** Plan1-4 + Q3 + Q4(已完成)
**后续:** 无(3-sub-project 最后一块)

**`_judge` 契约**(`eval/locomo/metrics.py:71-86`):`async def _judge(judge_llm, system, user) -> str`。两形态:① `LLMClient`(有 `.chat`)→ stream-collect done-content;② async fn(str)->str → `await judge_llm(system + "\n" + user)`。**返文本**,JSON 解析在调用点。test mock 用形态②最简:`async def fake(combined_str): return '{"score": 0.9}'`。

**pytest-asyncio auto mode**(`pyproject.toml: asyncio_mode = "auto"`):`async def test_*` 自动跑,无需 `@pytest.mark.asyncio`。

**关键约束**(spec review 沉淀):
- `evaluate_qa` 变 **async** → `test_evaluator.py` 现有 2 test(`test_evaluate_qa_returns_dict_with_expected_keys` L31 + `test_evaluate_qa_fail_when_low_f1_and_no_quality` L42)需改 `async def` + `await`(断言不变,`judge_llm=None` 默认 → semantic=None → pass=f1>0.5 旧 fallback,断言仍 hold)。
- pass = `semantic>0.7 if semantic is not None else f1>0.5`(fail-soft 退化路径无 `or quality>0.7`,因 quality 降诊断 decision #1 — spec 明确)。
- 不改 `token_f1` 算法 / 不降 CONTEXT_WINDOW / 不分桶 / 不去 quality(降诊断仍算仍展示)。

---

## File Structure(Q1 涉及)

| 文件 | 责任 | 改动 |
|---|---|---|
| `eval/locomo/evaluator.py` | QA 评分 | 新增 `semantic_f1`(async);`evaluate_qa` 变 async + pass 重构 + result dict 加 `semantic_f1` |
| `eval/locomo/metrics.py` | 纯聚合 | `compute_by_q_type` 加 `semantic_f1_med` |
| `eval/locomo/report.py` | HTML 报告 | `_summary_cards` 加 semantic-f1-median 卡;`_row` 加 semantic_f1 列;`_q_type_table` 加 semantic-f1-med 列;主表头加 semantic_f1 |
| `eval/locomo/runner.py` | eval 主循环 | `_run_sample` QA 循环 `await evaluate_qa(..., judge_llm=llm)` + result dict 加 `semantic_f1` |
| `eval/locomo/tests/test_evaluator.py` | evaluator unit | 改 2 现有 test async + 加 semantic_f1 / pass 重构 test |
| `eval/locomo/tests/test_metrics.py` | metrics unit | FIXTURE 加 semantic_f1 + test_compute_by_q_type 加 semantic_f1_med 断言 |
| `eval/locomo/tests/test_report.py` | report unit | fixture results 加 semantic_f1 + 断言 report 含 semantic_f1 |

---

## Task 1: `evaluator.py` semantic_f1 + evaluate_qa async 重构

**Files:** Modify `eval/locomo/evaluator.py`;Test `eval/locomo/tests/test_evaluator.py`

- [ ] **Step 1: 改 2 现有 evaluate_qa test 为 async**(`test_evaluator.py:31,42`):
```python
async def test_evaluate_qa_returns_dict_with_expected_keys():
    result = await evaluate_qa("What color?", "blue", "blue")  # judge_llm=None 默认
    assert set(result.keys()) >= {"f1", "semantic_f1", "quality", "pass", "trace_payload"}
    assert result["f1"] == 1.0
    assert result["pass"] is True              # f1>0.5 fallback(judge_llm=None → semantic None)
    assert result["semantic_f1"] is None        # 无 judge
    assert result["quality"] is None or 0.0 <= result["quality"] <= 1.0
    assert result["trace_payload"]["f1"] == result["f1"]

async def test_evaluate_qa_fail_when_low_f1_and_no_quality():
    result = await evaluate_qa("q", "completely wrong answer xyzzy", "the cat sat on the mat")
    assert result["f1"] < 0.3
    if result["quality"] is None:
        assert result["pass"] is False          # f1<0.5 fallback
```

- [ ] **Step 2: 加 semantic_f1 + pass 重构 test**(追加 test_evaluator.py):
```python
async def test_semantic_f1_equivalent():
    """judge 返 score=1.0 → semantic_f1 == 1.0。"""
    from eval.locomo.evaluator import semantic_f1
    async def fake_judge(s):  # _judge async-fn 形态:单 str 参数
        return '{"score": 1.0}'
    assert await semantic_f1("q", "blue", "blue", fake_judge) == 1.0

async def test_semantic_f1_partial():
    from eval.locomo.evaluator import semantic_f1
    async def fake_judge(s):
        return '{"score": 0.5}'
    assert await semantic_f1("q", "two cats", "two dogs", fake_judge) == 0.5

async def test_semantic_f1_judge_fail_soft():
    """judge 返非 JSON / raise → None。"""
    from eval.locomo.evaluator import semantic_f1
    async def bad_json(s):
        return "not json"
    async def raising(s):
        raise RuntimeError("judge down")
    assert await semantic_f1("q", "a", "b", bad_json) is None
    assert await semantic_f1("q", "a", "b", raising) is None

async def test_semantic_f1_no_llm():
    """judge_llm=None → None(退化 token_f1)。"""
    from eval.locomo.evaluator import semantic_f1
    assert await semantic_f1("q", "a", "b", None) is None

async def test_evaluate_qa_pass_semantic_main():
    """semantic>0.7 主(即使 f1 低)→ pass=True。"""
    async def fake_judge(s):
        return '{"score": 0.8}'
    # predicted 与 gold token 不重合(f1 低),但 semantic 高
    result = await evaluate_qa("q", "she went to paris", "she traveled to paris",
                               judge_llm=fake_judge)
    assert result["f1"] < 0.5                    # token 不重合
    assert result["semantic_f1"] == 0.8
    assert result["pass"] is True                # semantic>0.7 主

async def test_evaluate_qa_fail_soft_token_fallback():
    """semantic=None(judge_llm=None)+ f1>0.5 → pass=True(token 兜底)。"""
    result = await evaluate_qa("q", "the cat sat", "the cat sat on a mat")  # f1 偏高
    assert result["semantic_f1"] is None
    assert result["f1"] > 0.5
    assert result["pass"] is True
```

- [ ] **Step 3: 跑确认 FAIL**(`pytest eval/locomo/tests/test_evaluator.py -v` — `evaluate_qa` sync 调 await 报 TypeError / semantic_f1 不存在 ImportError)

- [ ] **Step 4: 实现 `evaluator.py`**:
  - 顶部 `import json`(若未有)+ `from eval.locomo.metrics import _judge`(复用;注意避免循环 import — evaluator 已被 metrics import?查:`metrics.py` 不 import evaluator,安全)。
  - 新增 `async def semantic_f1(prompt, predicted, gold, judge_llm) -> float | None`:
    - `judge_llm is None` → return None。
    - system = `"判 predicted answer 与 gold answer 语义是否等价(事实正确,忽略 phrasing/词形/语序)。返 JSON {\"score\": 0.0-1.0}(1.0=完全等价,0.5=部分对,0.0=错)。只返 JSON,不要其他文本。"`
    - user = `f"question: {prompt}\ngold: {gold}\npred: {predicted}"`。
    - `try: resp = await _judge(judge_llm, system, user); return float(json.loads(resp)["score"]) except Exception: return None`。
  - 重构 `async def evaluate_qa(prompt, predicted, gold, judge_llm=None) -> dict`:
    - `f1 = token_f1(predicted, gold)`(不动)。
    - `semantic = await semantic_f1(prompt, predicted, gold, judge_llm)`。
    - `quality = quality_score(prompt, predicted, gold)`(不动)。
    - `pass_ = (semantic > 0.7) if semantic is not None else (f1 > 0.5)`。
    - 返 `{"f1": f1, "semantic_f1": semantic, "quality": quality, "pass": pass_, "trace_payload": {"f1": f1, "semantic_f1": semantic, "quality": quality, "pass": pass_}}`。

- [ ] **Step 5: 跑 PASS**(`pytest eval/locomo/tests/test_evaluator.py -v` — 全 test 绿,含改的 2 + 新 6 = 8 evaluate_qa/semantic_f1 test + 6 原有 token_f1 test)

- [ ] **Step 6: 回归** `pytest eval/locomo/tests/ -q`(其他 locomo test 不破 — 但注意 runner test 调 evaluate_qa sync 会破,T4 修;本步先看 evaluator/metrics/report test)

- [ ] **Step 7: ruff** `.venv/Scripts/python.exe -m ruff check eval/locomo/evaluator.py`

- [ ] **Step 8: Commit**
```bash
cd D:/agent_learning/cc-harness
git add eval/locomo/evaluator.py eval/locomo/tests/test_evaluator.py
git commit -m "feat(locomo-eval): Q1 semantic_f1 + evaluate_qa async 重构 + pass 重构

evaluator.py:新增 semantic_f1(LLM judge 语义等价 0-1,复用 metrics._judge,fail-soft 返 None);evaluate_qa 变 async + 加 judge_llm 参数 + pass 重构(semantic>0.7 主 / token_f1>0.5 fail-soft 兜底)+ result dict 加 semantic_f1。token_f1/quality 算法不动。Q1 Task1。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `metrics.py` compute_by_q_type 加 semantic_f1_med

**Files:** Modify `eval/locomo/metrics.py`;Test `eval/locomo/tests/test_metrics.py`

- [ ] **Step 1: 改 FIXTURE + test_compute_by_q_type**(test_metrics.py:4-26):
  - FIXTURE 3 条 result 加 `"semantic_f1"` 字段(record0=0.85, record1=0.25, record2=None — 模拟部分 fail-soft)。
  - test_compute_by_q_type 加断言:
```python
def test_compute_by_q_type():
    from eval.locomo.metrics import compute_by_q_type
    out = compute_by_q_type(FIXTURE)
    assert "single-hop" in out and "multi-hop" in out
    sh = out["single-hop"]
    assert sh["n"] == 2
    assert sh["pass"] == 1
    assert "semantic_f1_med" in sh                       # 新列存在
    # single-hop 有 record0(0.85)+ record2(None)→ median of [0.85] == 0.85
    assert sh["semantic_f1_med"] == pytest.approx(0.85)
```

- [ ] **Step 2: 跑 FAIL**(`pytest eval/locomo/tests/test_metrics.py::test_compute_by_q_type -v` — KeyError "semantic_f1_med")

- [ ] **Step 3: 实现 `metrics.py:compute_by_q_type`**(~L15-23,加 semantic_f1_med):
```python
sem = [r["semantic_f1"] for r in rs if r.get("semantic_f1") is not None]
out[qt] = {
    "n": len(rs),
    "f1_med": st.median(f1) if f1 else None,
    "semantic_f1_med": st.median(sem) if sem else None,   # 新
    "quality_med": st.median(q) if q else None,
    "pass": sum(1 for r in rs if r.get("pass")),
}
```

- [ ] **Step 4: 跑 PASS**(test_compute_by_q_type 绿;其他 metrics test 不破 — FIXTURE 加字段向后兼容)

- [ ] **Step 5: ruff** `.venv/Scripts/python.exe -m ruff check eval/locomo/metrics.py`

- [ ] **Step 6: Commit**
```bash
cd D:/agent_learning/cc-harness
git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git commit -m "feat(locomo-eval): Q1 compute_by_q_type 加 semantic_f1_med 列

metrics.py:compute_by_q_type 加 semantic_f1_med(median of r['semantic_f1'],无值→None)。FIXTURE 加 semantic_f1 字段。Q1 Task2。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `report.py` 3 处加 semantic_f1 列/卡

**Files:** Modify `eval/locomo/report.py`;Test `eval/locomo/tests/test_report.py`

- [ ] **Step 1: 加 test + fixture 加字段**(test_report.py):
  - 现有 fixture results(各 test)加 `"semantic_f1"` 字段(test_write_html_report_creates_file/test_summary_cards_appear 等 — 至少 1 个加 0.8 演示)。
  - 加断言 + 新 test:
```python
def test_summary_cards_semantic_median(tmp_path):
    """_summary_cards 含 semantic-f1-median 卡。"""
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "x", "status": "ok",
         "f1": 0.1, "semantic_f1": 0.8, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001, "tool_calls": []},
    ]
    write_html_report(results, tmp_path / "r.html")
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "semantic-f1-median" in text

def test_row_has_semantic_f1_col(tmp_path):
    """主表 _row 含 semantic_f1 列。"""
    results = [
        {"sample_id": "s1", "turn_idx": -1, "q_type": "x", "status": "ok",
         "f1": 0.1, "semantic_f1": 0.85, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001, "tool_calls": []},
    ]
    write_html_report(results, tmp_path / "r.html")
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "0.850" in text            # semantic_f1 格式化

def test_q_type_table_has_semantic_col(tmp_path):
    """_q_type_table 表头含 semantic-f1-med。"""
    metrics = {"by_q_type": {"x": {"n": 1, "f1_med": 0.1, "semantic_f1_med": 0.8, "quality_med": 0.6, "pass": 1}},
               "compaction": {"triggered": 0, "by_tier": {}, "avg_retain": None},
               "utilization": {"avg": 0.0, "peak": 0.0},
               "token_series": {"prompt": [], "completion": [], "cumulative_cost": 0},
               "memory": "uncomputed", "tool_accuracy": "uncomputed"}
    write_html_report([], tmp_path / "r.html", metrics=metrics)
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "semantic-f1-med" in text
```

- [ ] **Step 2: 跑 FAIL**

- [ ] **Step 3: 实现 `report.py`**:
  - `_summary_cards`(~L40-44):加 `sem_vals = sorted(r["semantic_f1"] for r in results if r.get("semantic_f1") is not None)` + `sem_med = sem_vals[len(sem_vals)//2] if sem_vals else 0.0`;cards 列表加 `("semantic-f1-median", f"{sem_med:.3f}")`(放 f1-median 之后)。
  - `_row`(~L86-98):cells 加 `f"{r.get('semantic_f1',''):.3f}" if r.get("semantic_f1") is not None else "-"`(放 f1 之后)。
  - `_q_type_table`(~L106-124):循环加 `sm = st.get("semantic_f1_med") ...` + `<td>{'-' if sm is None else f'{sm:.3f}'}</td>`(放 f1-med 后);表头加 `<th>semantic-f1-med</th>`。
  - 主表表头(~L209-212):加 `<th>semantic_f1</th>`(放 f1 后)。

- [ ] **Step 4: 跑 PASS**(3 新 test + 现有 report test 不破 — fixture 加字段向后兼容)

- [ ] **Step 5: ruff** `.venv/Scripts/python.exe -m ruff check eval/locomo/report.py`

- [ ] **Step 6: Commit**
```bash
cd D:/agent_learning/cc-harness
git add eval/locomo/report.py eval/locomo/tests/test_report.py
git commit -m "feat(locomo-eval): Q1 report 加 semantic_f1 卡/列/分桶列

report.py:_summary_cards 加 semantic-f1-median 卡;_row 加 semantic_f1 列;_q_type_table 加 semantic-f1-med 列;主表头加 semantic_f1。fixture 加 semantic_f1。Q1 Task3。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: `runner.py` await evaluate_qa + result dict 加 semantic_f1

**Files:** Modify `eval/locomo/runner.py`;Test `eval/locomo/tests/test_runner_resume.py`(若有 runner unit)或集成验证

- [ ] **Step 1: 跑确认 FAIL**(`pytest eval/locomo/tests/ -q` — runner QA 循环调 `evaluate_qa(...)` sync 现在 evaluate_qa 是 async → 返 coroutine 未 await;或 test_runner_smoke 集成;先看是否 test 直接覆盖。若无 runner unit test 直接覆盖 QA 循环,本 task 依赖 T5 集成验)

- [ ] **Step 2: 改 `runner.py:_run_sample`**(~L259-275,QA 循环):
  - `eval_result = evaluate_qa(qa.question, predicted, qa.answer)` → `eval_result = await evaluate_qa(qa.question, predicted, qa.answer, judge_llm=llm)`(runner 已有 `llm` ~L181,in scope)。
  - result dict 加 `"semantic_f1": eval_result["semantic_f1"],`(放 f1 后)。
  - 确认 QA 循环已在 `async def _run_sample` 内(await 合法)— 是。

- [ ] **Step 3: 跑 PASS**(`pytest eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q` — runner 相关 test + 全 locomo test 绿;smoke 需真 LLM 排除)

- [ ] **Step 4: ruff** `.venv/Scripts/python.exe -m ruff check eval/locomo/runner.py`(E402 pre-existing 除外)

- [ ] **Step 5: Commit**
```bash
cd D:/agent_learning/cc-harness
git add eval/locomo/runner.py
git commit -m "feat(locomo-eval): Q1 runner QA 循环 await evaluate_qa + result dict 加 semantic_f1

runner.py:_run_sample QA 循环 evaluate_qa 加 await + judge_llm=llm(evaluator 已变 async);result dict 加 semantic_f1 字段。Q1 Task4。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 全回归 + locomo 烟测(controller 验 import + unit;locomo 真跑用户)

- [ ] **Step 1: import 冒烟** `python -c "from eval.locomo.evaluator import semantic_f1, evaluate_qa; from eval.locomo.metrics import compute_by_q_type; from eval.locomo.report import write_html_report; print('ok')"`
- [ ] **Step 2: 全回归** `pytest tests/ eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q`(Q4 相关 + locomo 全绿;11 pre-existing promptfoo 失败 Q1 无关,确认)
- [ ] **Step 3: ruff 全** `.venv/Scripts/python.exe -m ruff check eval/locomo/`
- [ ] **Step 4: locomo 烟测**(用户跑,真 LLM):`$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python.exe eval\locomo\runner.py --limit 1 --output-dir eval\result\locomo-q1-smoke` → 看 pass 分布(semantic_f1 主)+ by_q_type semantic_f1_med + report 含 semantic_f1 列

---

## Q1 完成标准

- [ ] Task 1-4 全 commit,`pytest eval/locomo/tests/`(除 smoke)全绿
- [ ] `ruff check eval/locomo/` 干净(E402 pre-existing runner 除外)
- [ ] semantic_f1:LLM judge 0-1 + fail-soft(None)+ no-llm 退化
- [ ] evaluate_qa async + pass 重构(semantic>0.7 主 / f1>0.5 fail-soft 兜底)
- [ ] token_f1 算法不动(辅)+ quality 不动(诊断)
- [ ] compute_by_q_type 加 semantic_f1_med
- [ ] report 3 处(卡/列/分桶)加 semantic_f1
- [ ] runner QA 循环 await + result dict 加 semantic_f1
- [ ] 2 现有 evaluate_qa test 改 async(断言不变 hold)
- [ ] Q4 + Q3 test 不破
- [ ] 不降 CONTEXT_WINDOW / 不分桶 / 不改 token_f1 算法 / 不去 quality

## Q1 完成后(3-sub-project 进度)

- Q3 长期分层 ✅
- Q4 短期卸载 ✅
- **Q1 指标公允 ✅(本 plan)** — 3-sub-project 全完成
