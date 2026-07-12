# Q1 指标公允(semantic f1 + pass 重构)设计

> **范围**:locomo 评测指标公允化。本 spec 是 3-sub-project 重建的**第 3 块(最后)**(Q1)。Q3(长期分层)✅、Q4(短期卸载)✅ 已完成;各自独立 spec。
>
> **痛点来源**:locomo conv-26 烟测 pass 39%、token_f1 median 0.032。根因诊断:token_f1 严格 token overlap,LLM 自由 phrasing(同义/词形/语序)即使语义等价也压低分;quality(deepeval GEval)fail-soft(无 deepeval / judge 挂 → None),pass 兜不住。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

加 **semantic_f1**(LLM judge 语义等价分,continuous 0-1)当主指标,替代 token_f1 主位;重构 `pass` 判定(semantic_f1 > 0.7 主 + token_f1 fail-soft 兜底)。token_f1 降辅、quality(GEval)降诊断。让 pass/f1 反映真实答案质量而非 token 字面匹配。

## 背景(为何做)

locomo token_f1 是官方指标,但严格 overlap:CJK 逐字 + `[a-zA-Z0-9]` 词,LLM 自由 phrasing(`gold="2023年3月"` vs `pred="2023 年三月"` / `gold="她去了巴黎"` vs `pred="她前往巴黎"`)token 集合差异大 → f1 低,即使语义等价。median 0.032 说明**大部分 QA phrasing 与 gold 不重合**,token_f1 无法反映真实正确性。

业界(locomo 后续工作 + LLM-as-judge 范式)用 **semantic correctness**(LLM judge pred 与 gold 语义是否等价)更公平。Q1 引入 semantic_f1 当主指标,直接修 token_f1 严格压低的根因。

## 现有代码事实(Q1 落点,锚点描述 — 行号易漂移以锚点为准)

| 文件 | 现状 | Q1 处置 |
|---|---|---|
| `eval/locomo/evaluator.py:token_f1` | whitespace+CJK per-char+`[a-zA-Z0-9]` token,严格 overlap | **不动算法**,降辅(report 仍展示,pass 不主依赖) |
| `eval/locomo/evaluator.py:quality_score` | deepeval GEval("answer quality"),fail-soft 返 None | **不动算法**,降诊断(算但不进 pass) |
| `eval/locomo/evaluator.py:evaluate_qa` | `pass = f1>0.5 or quality>0.7`(OR 关系) | **重构**:`pass = semantic_f1>0.7`;fail-soft(semantic_f1 None)→ 退化 `token_f1>0.5` |
| `eval/locomo/evaluator.py` | 无 semantic_f1 | **新增** `semantic_f1(prompt, predicted, gold) -> float\|None`(LLM judge,复用 `_judge` stream-collect 模式) |
| `eval/locomo/metrics.py:_judge` | async judge helper(支持 LLMClient.chat / async fn 两形态),JSON 解析 | **复用**(semantic_f1 judge 调它) |
| `eval/locomo/metrics.py:compute_by_q_type` | 按 q_type 分桶 f1_med/quality_med/pass | **加列** semantic_f1_med |
| `eval/locomo/metrics.py:run_judge` | 编排纯聚合 + 离线 judge,缓存 | semantic_f1 是否进离线 judge?**否**(semantic_f1 在 evaluator 在线算 per-qa,进 result dict;run_judge 纯聚合读它) |
| `eval/locomo/runner.py:_run_sample` | QA 循环调 `evaluate_qa`,result dict 含 f1/quality/pass | **加字段** `semantic_f1` 进 result dict |
| `eval/locomo/report.py:_summary_cards` | 6 base + 4 metrics 卡(f1-median/quality-median) | **加卡** semantic-f1-median |
| `eval/locomo/report.py:_row` | 主表行(f1/quality/pass 列) | **加列** semantic_f1 |
| `eval/locomo/report.py:_q_type_table` | 分桶表(q_type/n/f1-med/quality-med/pass) | **加列** semantic-f1-med |

## 关键决策(brainstorm 确认)

1. **双指标栈**:semantic_f1(主,新)+ token_f1(辅,现)。quality(GEval)降诊断(算但不进 pass)。**不**用三指标栈(judge 调用 2/qa 贵);**不**改 quality criteria 当主(名误导)。
2. **semantic_f1 continuous 0-1**:与 token_f1 同尺度可比,业界 LLM-as-judge 标准。**不**用 binary(全对/全错丢细粒度)/ 3-tier(离散与 token_f1 不好融合)。
3. **pass = semantic_f1 > 0.7**(主)。阈值 0.7 对齐现 quality 阈值 + 业界 LLM-judge 常用。
4. **fail-soft 退化**:semantic_f1 None(judge 挂 / 无 OPENAI_API_KEY / judge 返非 JSON)→ `pass = token_f1 > 0.5`(旧逻辑兜底,保底不断评)。
5. **judge 复用 `_judge` helper**(metrics.py):semantic_f1 在 evaluator 在线算(per-qa,QA 循环内),**不**进 run_judge 离线批(避免两次 judge 路径)。result dict 存 semantic_f1,run_judge 纯聚合读。
6. **judge LLM 复用 OPENAI_MODEL(deepseek)**:runner QA 循环已构造 `llm`,传给 evaluate_qa。
7. **缓存**:semantic_f1 judge 调用贵;**进现有 judge cache**(run_judge 的 `judge-cache.json`)?**否** — semantic_f1 在线算(runner QA 循环),结果进 result dict 落 `locomo-results.json`,本身即缓存(resume 读)。不另加 cache 层。
8. **quality(GEval)保留**:仍算(诊断维度,report 展示),但不进 pass。semantic_f1 涵盖 correctness,quality 沦"answer 质量/流畅"辅助视角。
9. **不改 token_f1 算法**:降辅非删,report 仍展示(公平性 baseline,与 semantic_f1 对照看 phrasing gap)。

## 架构

```
[QA 循环(runner._run_sample)]
   └─ evaluate_qa(question, predicted, gold, llm)   ← 加 llm 参数
        ├─ f1 = token_f1(predicted, gold)            ← 不动(辅)
        ├─ semantic = semantic_f1(question, predicted, gold, llm)  ← 新(主)
        │     └─ _judge(llm, system="判语义等价返 JSON{score:0-1}",
        │              user="gold:...\npred:...")    ← 复用 metrics._judge
        │     └─ JSON 解析 score;fail-soft 返 None
        ├─ quality = quality_score(...)              ← 不动(诊断)
        └─ pass = semantic>0.7 if semantic is not None else f1>0.5  ← 重构
   └─ result dict 加 "semantic_f1": semantic

[metrics.run_judge 纯聚合]
   └─ compute_by_q_type 加 semantic_f1_med 列(读 result["semantic_f1"])

[report]
   └─ _summary_cards 加 semantic-f1-median 卡
   └─ _row 加 semantic_f1 列
   └─ _q_type_table 加 semantic-f1-med 列
```

## 组件(改动)

### `eval/locomo/evaluator.py`
- **新增** `async def semantic_f1(prompt, predicted, gold, judge_llm) -> float | None`:
  - 复用 `metrics._judge`(import 或内联 stream-collect;`_judge` 在 metrics.py,evaluator 复用 import)。
  - system prompt:"判 predicted answer 与 gold answer 语义是否等价(事实正确,忽略 phrasing/词形/语序)。返 JSON `{\"score\": 0.0-1.0}`(1.0=完全等价,0.5=部分对,0.0=错)。"
  - user:`f"question: {prompt}\ngold: {gold}\npred: {predicted}"`。
  - JSON 解析 `score`;解析失败 / judge 挂 → 返 None(fail-soft)。
- **重构** `async def evaluate_qa(prompt, predicted, gold, judge_llm=None) -> dict`:
  - 加 `judge_llm` 参数(默认 None,无 judge 退化 token_f1)。
  - `f1 = token_f1(predicted, gold)`(同步,不动)。
  - `semantic = await semantic_f1(...)` if judge_llm else None。
  - `quality = quality_score(...)`(同步 GEval,不动)。
  - `pass_ = (semantic > 0.7) if semantic is not None else (f1 > 0.5)`。
  - 返 dict 加 `"semantic_f1": semantic`。
  - **变 async**(semantic_f1 是 async judge 调用)。

### `eval/locomo/runner.py:_run_sample`
- QA 循环 `evaluate_qa(qa.question, predicted, qa.answer)` → `await evaluate_qa(qa.question, predicted, qa.answer, judge_llm=llm)`(runner 已有 `llm`,传它)。
- result dict 加 `"semantic_f1": eval_result["semantic_f1"]`。

### `eval/locomo/metrics.py:compute_by_q_type`
- 加 `semantic_f1_med`(median of `r["semantic_f1"]`)。

### `eval/locomo/report.py`
- `_summary_cards`:加 `semantic-f1-median` 卡(median of `r["semantic_f1"]`)。
- `_row`:加 semantic_f1 列(`{r.get('semantic_f1',''):.3f}` or "-")。
- `_q_type_table`:加 `semantic-f1-med` 列 + 表头 `<th>semantic-f1-med</th>`。
- 主表表头加 `<th>semantic_f1</th>`。

## 数据流

```
[QA predicted = run_turn 最后 message content]
  └─ await evaluate_qa(question, predicted, gold, judge_llm=llm)
       ├─ f1 = token_f1(...)                         # 辅,同步
       ├─ semantic = await semantic_f1(..., llm)     # 主,async judge
       │     └─ _judge(llm, system, user) → JSON {score} → float
       │     └─ fail-soft: None
       ├─ quality = quality_score(...)               # 诊断,同步 GEval
       └─ pass = semantic>0.7 if semantic!=None else f1>0.5
  └─ result["semantic_f1"] = semantic
  └─ runner 写 locomo-results.json(含 semantic_f1,resume 即缓存)
[metrics.run_judge(results, ...)]
  └─ compute_by_q_type → semantic_f1_med 列
[report.write_html_report(results, metrics)]
  └─ _summary_cards 加 semantic-f1-median 卡
  └─ _row 加 semantic_f1 列
  └─ _q_type_table 加 semantic-f1-med 列
```

## 测试策略

### 位置
- `tests/test_locomo_evaluator.py`(新 / 已有?查)— evaluator unit(mock judge_llm)
- `tests/test_locomo_metrics.py`(已有)— compute_by_q_type 加 semantic_f1_med
- `tests/test_locomo_report.py`(已有)— _row/_q_type_table/_summary_cards 加列

### Unit
- `test_semantic_f1_equivalent` — judge mock 返 `{"score": 1.0}` → semantic_f1 == 1.0
- `test_semantic_f1_partial` — judge mock 返 `{"score": 0.5}` → 0.5
- `test_semantic_f1_judge_fail_soft` — judge mock 返非 JSON / raise → None
- `test_semantic_f1_no_llm` — judge_llm=None → None(退化 token_f1)
- `test_evaluate_qa_pass_semantic_main` — semantic=0.8, f1=0.1 → pass=True(semantic 主)
- `test_evaluate_qa_fail_soft_token_fallback` — semantic=None, f1=0.6 → pass=True(token 兜底)
- `test_evaluate_qa_fail_soft_token_low` — semantic=None, f1=0.1 → pass=False
- `test_compute_by_q_type_semantic_med` — results 含 semantic_f1 → by_q_type 有 semantic_f1_med
- `test_report_row_has_semantic_f1_col` — _row 输出含 semantic_f1 单元格
- `test_report_q_type_table_has_semantic_col` — _q_type_table 表头含 semantic-f1-med
- `test_report_summary_card_semantic_median` — _summary_cards 含 semantic-f1-median 卡

### 现有不破
- `token_f1` 算法测试不变(算法不动)
- `quality_score` 测试不变(算法不动)
- `evaluate_qa` 现有测试:加 judge_llm=None 默认 → 行为对齐旧(semantic=None → pass=f1>0.5)
- runner / metrics / report 现有测试不破

## 非目标(out of scope)

- **token_f1 算法改动**(降辅非改算法)
- **q_type 分桶 / 分类型阈值**(YAGNI,先核心指标公允;分桶诊断是后续)
- **降 CONTEXT_WINDOW**(Q1 在正常窗口下做,不靠降窗口作弊;用户明确)
- **quality(GEval)算法改动 / 去掉**(降诊断,仍算仍展示)
- **locomo 数据集 / runner 主循环 / Q3/Q4 记忆改动**

## 风险

1. **semantic_f1 judge 主观性 / 方差**:LLM judge 同一 (pred, gold) 多次评分可能波动。缓解:criteria 明确(语义等价 + 返 0-1 + 部分对 0.5 示例)+ 复用 deepseek(便宜稳)+ 缓存(result json,resume 不重判)。
2. **judge 调用成本**:每 QA 1 次 judge(locomo 几百 QA × judge)。缓解:result 落 json 即缓存(resume 读);Q4 offload 已减 context token(间接省 judge input)。无 key 时 fail-soft(None)退化 token_f1,不阻断。
3. **evaluate_qa 变 async**:runner QA 循环已 async,加 await 即可;但若有同步调用点(测试)需改。缓解:测试 mock judge_llm 异步。
4. **pass 阈值 0.7 校准**:0.7 是经验值(对齐 quality),真实 locomo 分布可能需调。缓解:Q1 完成后烟测看 pass 分布 + by_q_type semantic_f1_med,必要时调阈值(本 spec 不含校准循环,YAGNI 先 0.7)。
5. **semantic_f1 与 quality 重叠**:都评 correctness。缓解:criteria 区分(semantic_f1 评"语义等价/事实对",quality 评"answer 质量/相关/流畅");report 双展示对照。

## 实现顺序(writing-plans 细化)

1. `evaluator.py`:`semantic_f1` 函数(LLM judge + fail-soft)+ `evaluate_qa` 重构(async + pass = semantic>0.7 主 / token_f1 兜底)+ result dict 加字段
2. `metrics.py:compute_by_q_type`:加 semantic_f1_med
3. `report.py`:`_summary_cards` 加卡 + `_row` 加列 + `_q_type_table` 加列 + 主表头
4. `runner.py:_run_sample`:QA 循环传 judge_llm + await + result dict 加 semantic_f1
5. 全回归 + locomo 烟测(controller 验 import + unit;locomo 真跑用户)
