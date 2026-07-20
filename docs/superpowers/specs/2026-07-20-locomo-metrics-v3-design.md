# Locomo 长对话评测 — 指标体系 v3(5 维度重排)

- 日期:2026-07-20
- 状态:**设计稿**(待 user 审 → 转 plan)
- 子项目编号:**M5-2(eval 多工具化,子项目 2)** — 紧跟 M5-1(2026-07-07 spec 已落地)
- 上承:`docs/superpowers/specs/2026-07-07-locomo-eval-design.md`(M5-1,把 memory 工具注入 + LLM 跑通)

---

## 1. 背景与目标

### 1.1 现状(M5-1 落地后)

`eval/locomo/` 已能:
- 跑 LoCoMo10 全 10 个对话,1986 条 QA
- per-record 评分:f1(token) / semantic_f1(LLM judge) / quality(GEval) / pass
- 聚合:by_q_type / compaction / utilization / token_series / memory(P&R) / tool_accuracy
- 报告 HTML:5+1 张顶层卡 + q_type 分桶表
- trace 上 langfuse

5 张顶层卡(Q不 / Utilization / Compaction / Memory-R / Memory-P / Tool-Acc)+ 旧的 by_q_type 表,**与用户对长对话 agent 的关注点不直接对齐**:

- 用户真正关心的是:**agent 在长对话里是不是真的记准了 / 知识覆盖了旧信息 / 用得不多余 / 压缩后还能答对 / 多轮里不分心**。
- 当前指标体系里:#1 记忆召回 precision/recall 是用 gold evidence vs memory_recall 返回(只精确到全 conv,不分是不是当前会话);#2 时效性没有专门维度;#3 上下文利用率只看 token / window(体积,非"有用度");#4 压缩是 trigger 计数,没有 pass 联合;#5 多轮一致性根本没有。
- 工具准确率、GEval quality 与 5 个用户关注点正交,可降级为 trace 调试字段。

### 1.2 目标(GOAL)

**把 `eval/locomo/` 的指标体系重排,只服务 5 个轴:记忆召回准确率、时效性、上下文利用率、上下文压缩率、多轮一致性。其它旧指标全部降级为 trace 内部 / langfuse 调试用,不再出顶层报告卡。**

完成定义:
1. `evaluator.py` per-record schema 支持 #3(`chunk_usefulness`),其它旧字段保留
2. `metrics.py` 提供 5 个新 aggregate 函数(共享 judge_llm 编排 + 磁盘缓存)
3. `report.py` 顶层只出 5 张卡 + 各自 sub-table;raw per-record 折叠
4. runner.py 改 1 行(多传 `messages=` 给 evaluate_qa),其它 runner / agent.py / dataset.py 不动
5. `policy_local.yaml` 加 `metrics_v3: true` 与 `judge_chunk_usefulness: true` 作开关,**双轨跑一个版本周期**,支持回滚
6. 9 个 unit test 全过(5 指标函数 + chunk judge + report)
7. 全量 10 样本跑通,HTML 报告结构与设计一致

### 1.3 不在范围(OUT)

- ❌ 改 LoCoMo10 数据集(时序覆盖 / 一致性 暂时不构造 correction pairs)
- ❌ 改 agent.py / `run_turn` / memory tools(沿用 M5-1 不变)
- ❌ 改 runner.py 主循环(只传 1 个 kwarg)
- ❌ 加新依赖(`transformers` / `scikit-learn` / `plot.js` 等都不引)
- ❌ 给 LoCoMo 加新数据集(沿用 10 个 conversation)
- ❌ 跑其它数据集(FEVER/FreshQA 不接)
- ❌ 做时间序列折线图(避免 plot.js 重依赖)
- ❌ 写实现计划(待 spec 审完 → writing-plans skill)

### 1.4 验收标准(AC)

| AC | 内容 |
|---|---|
| AC1 | `python eval/locomo/runner.py --limit 1` 跑通,产出 HTML,5 张卡 + sub-table 全显示(模板套好) |
| AC2 | 全量 10 样本跑完,`policy_local.yaml: metrics_v3: true` 时新指标全填;`false` 时旧 cards 仍出 |
| AC3 | judge_llm=None 时 #1 #5 显示 `uncomputed`;其它指标仍填 |
| AC4 | `pytest eval/locomo/tests/ -v` 通过(包含 9 个新测试) |
| AC5 | chunk_usefulness 在 evaluator.py 算,不在 runner 加新 trace 调用 |
| AC6 | 缓存命中:同一数据集二次跑不再发 LLM judge |

---

## 2. 5 个指标算法定义

### 2.1 指标 1:记忆召回准确率

**定义**:在 LoCoMo10 一个 conversation 内,**所有 gold evidence 都落在同一 session 的 QA**,其 `memory_recall` 工具召回中,跟 gold evidence 相关的比例。

**输入**:
- `results`:list[record],每个 record 含 `tool_calls: [{name, args, result}]`
- `qas`:对应 `eval_qa`,每个含 `evidence: ["D1:3", ...]`
- `conversations`:原始 LoCoMo conversation(给 evidence → session 映射)
- `judge_llm`:LLMClient | None

**算法**:
1. 对每条 qa:调用 `dataset.build_session_index(conversation)` 得 `{ref → session_name}`。判断所有 evidence 是否在同一 session。**只在子集 `n_eligible` 上**继续。
2. 对每条 eligible qa:抽 `memory_recall` tool call 的 `result` 文本(可能有多个 call)。
3. 对每条 evidence 调 judge:{memory_text, evidence} → `{relevant: bool}`;同 evidence × recall_text pair 也判(relevance 双向 cache-able)
4. `precision = (relevant_count) / (total_recall_returns)`;`recall = (evidence_covered) / (n_evidence)`

**输出**:
```python
{"n_eligible": int, "n_total_recall": int, "precision": float, "recall": float}
```

**判定阈值(plan 阶段定型,spec 只列位)**:`relevant` bool 由 judge 决定。

**失败**:
- `judge_llm is None` → `"uncomputed"`
- per-pair `judge` 抛 → fail-soft,该 pair 跳过

### 2.2 指标 2:时效性

**定义**:LoCoMo10 `category=3`(Temporal,96 条)的 QA 通过率,把"Temporal" 作为"时序点正确性"的代理。后续如要严格"覆盖旧事实",在不破坏数据集前提下用这条子集观察。

**输入**:仅 `results`。

**算法**:
1. 过滤 `r["q_type"] == "3"`
2. `pass_rate = sum(r["pass"] for r in subset) / n`
3. `f1_med = median(r["f1"] for r in subset)`,`semantic_f1_med = median(...)`

**输出**:
```python
{"n": int, "pass_rate": float | None, "f1_med": float | None, "semantic_f1_med": float | None}
```

**失败**:`n == 0` → 上述字段全 `None`,**不报错误**(后续若 dataset 换了没 Temporal 也是合法状态)。

**注**:不构造新 fact-correction 对、不引新数据集。在 spec 范围外。

### 2.3 指标 3:上下文利用率

**定义**:对一条 QA 的最终 prompt,judge 标记每段 context chunk 是否对回答该 QA "有贡献"(yes / minor / no 三档),**useful token = sum(chunk.tokens × chunk.useful_score)**,利用率 = useful / prompt_tokens。

**`chunk` 切分**(evaluator.py 跑 chunk judge 时):
- `role=system` → 1 个 chunk
- 每个 `role=user`(对话历史 + 最终 question) → 1 个 chunk
- 每个 `role=tool`(tool result,含 memory_recall 返回) → 1 个 chunk
- 跳过 `role=assistant`(本身是 LLM 输出,不是"context")

**judge prompt**(占位):
```
Judge chunk usefulness for this QA:
  chunk: {chunk_content}
  question: {qa_question}
  gold_answer: {qa_gold}
返 JSON {"useful": "yes" | "minor" | "no"}。
```

score 映射:yes=1.0,minor=0.5,no=0.0。

**输入**:`results`(每个 record 含 `chunk_usefulness: [{role, tokens, useful_score}]`)+ `judge_llm`(已缓存在 record 上,metrics 层只聚合)

**算法**(纯聚合,无 judge):
1. 对每 record:weighted_useful = sum(s["tokens"] × s["useful_score"] for s in r["chunk_usefulness"])
2. ratio = weighted_useful / r["prompt_tokens"]
3. 跨 record:avg / p50 / p90

**输出**:
```python
{"avg": float, "p50": float, "p90": float, "min": float, "max": float, "n": int}
```

**失败**:`chunk_usefulness` 全空 → `"uncomputed"`。

**缓存**:evaluator 抽 chunk 后,以 `(sample_id, role, sha256(content)[:16])` 为 key;同 chunk 在同 conversation 跨多条 QA 复用(对话历史大量重复)。

### 2.4 指标 4:上下文压缩率

**定义**:按 compression tier 分桶(record.compaction.tier,0/1/2/3),每桶 `trigger_n / avg_retain / pass_rate`。

**`avg_retain`**:该 tier 所有 record 的 `mean(compaction.after_tokens / compaction.before_tokens)`(仅在 before/after 都存在时)。

**输入**:仅 `results`。runner 已产出 `compaction.{tier, before_tokens, after_tokens}`(M5-1)。

**算法**:
1. 对每 record:r["compaction"] 可能是 None;tier 默认 0(未压缩)。
2. 按 tier 桶 group:`trigger_n`、`avg_retain`、`pass_rate = sum(pass)/n`

**输出**:
```python
{
  "by_tier": [
    {"tier": 0, "trigger_n": int, "avg_retain": None,       "pass_rate": float},
    {"tier": 1, "trigger_n": int, "avg_retain": float,      "pass_rate": float},
    {"tier": 2, "trigger_n": int, "avg_retain": float,      "pass_rate": float},
    {"tier": 3, "trigger_n": int, "avg_retain": float,      "pass_rate": float},
  ],
  "total_compressed_n": int,  # tier >= 1
  "overall_avg_retain": float | None,
}
```

**失败**:`compaction` 字段全 None → by_tier 全 0,overall=None。

### 2.5 指标 5:多轮一致性

**定义**:在同 conversation 内,**同一 key entity 反复出现于 ≥2 个 gold_answer 的 QA** → 这些 QA 的 predicted 一起 judge 是否"对该 entity 都说一致"。`drift_rate = 不一致 entity 组数 / 总 entity 组数`。

**算法**:
1. `by_sample = group(results, key=sample_id)`
2. 对每 record,judge "extract entities from gold":`judge_entities(gold)` → `[entity1, entity2, ...]`,cache by `(sample_id, qa_id)`
3. 聚合到 conversation 级:`{entity: [predicted_answers...]}`(只保留 ≥2 record 的 entity)
4. 对每 (entity, group) 调 judge_consistency:`{consistent: bool, reason: str}`,cache by `(sample_id, entity, sorted_qa_ids)`
5. drift_rate across all groups

**输入**:`results`(按 sample_id 分组)、`judge_llm`。

**输出**:
```python
{
  "n_groups": int,
  "drift_rate": float,
  "by_sample": [
    {"sample_id": str, "n_groups": int, "drift_groups": int, "drift_rate": float},
    ...
  ],
}
```

**失败**:`judge_llm is None` → `"uncomputed"`;per-judge fail-soft skip。

---

## 3. 架构

### 3.1 数据流(单 QA)

```
runner.py (M5-1 不变,只多传 1 kwarg):
    │
    ├── 对每 QA record:走 evaluate_qa(question, predicted, gold, *,
    │     messages=qa_messages, judge_llm=llm)
    │     │
    │     └── evaluator.py:
    │           ├── token_f1 + semantic_f1 + pass(原样)
    │           ├── 抽 messages → chunks(系统 / user / tool)
    │           ├── judge_chunk_usefulness(per chunk) → cache → 0/0.5/1
    │           ├── 返回 {f1, semantic_f1, quality, pass, chunk_usefulness}
    │           │
    │     └── runner 把返回 append 到 results,与 M5-1 字段一致 + chunk_usefulness
    │
    └── 跑完 1986 QA:
          │
          └── metrics.run_judge(results, qas, conversations, judge_llm, cache_path):
                │
                ├── 并行/串行调 5 个 compute_*
                ├── 调过 judge 的写 .report-cache/locomo-judge-<dataset_sha8>.json
                │
                └── 返回 5-key dict
                      ↓
                  report.write_html_report(...)
                  → 5 卡 + 5 sub-table + 折叠 raw
```

### 3.2 模块清单(M5-2 改动表)

| 路径 | 改动 | 来源 |
|---|---|---|
| `eval/locomo/evaluator.py` | `evaluate_qa` 加 `messages=None` kwarg;返回加 `chunk_usefulness`;新增 `judge_chunk_usefulness` | 改 |
| `eval/locomo/metrics.py` | 删:`compute_by_q_type` / `compute_memory` / `compute_tool_accuracy`(并入 #1);新增 5 个函数;`run_judge` 编排重写 | 改 |
| `eval/locomo/report.py` | `_summary_cards` → `_summary_cards_v3`;新增 5 sub-table;raw raw_records_table 折叠 | 改 |
| `eval/locomo/dataset.py` | 新增 `build_session_index` 函数(无副作用) | 加 1 函数 |
| `eval/locomo/runner.py` | `_process_one_qa` 改 1 行:`evaluate_qa` 多传 `messages=qa_messages` | 改 1 行 |
| `eval/locomo/policy_local.yaml` | 加 `metrics_v3` + `judge_chunk_usefulness` 2 个开关 | 改 keys |
| `eval/locomo/tests/test_metrics_v3.py` | 9 个新 test | 新建 |
| `eval/locomo/tests/test_evaluator_v3.py` | 4 个新 test(签名 + 回归) | 新建 |
| `eval/locomo/tests/test_report.py` | 更新 `_summary_cards_v3` 渲染 case | 改 |
| `eval/locomo/tests/test_runner_smoke.py` | 不动(M5-1 已覆盖主路径) | — |
| `cc_harness/` | **不动** | — |
| `data/locomo10.json` | **不动**(沿用) | — |

### 3.3 per-record schema(终态)

```python
{
    # —— runner 已产(M5-1)——
    "sample_id": str,
    "q_type": str,                     # str(category),#2 用
    "question": str,
    "predicted": str,
    "gold": str,
    "pass": bool,                      # #4 pass_rate 来源
    "prompt_tokens": int,              # #3 分母 / token series
    "completion_tokens": int,
    "cost_usd": float,
    "tool_calls": [{"name", "args", "result"}],   # #1 取 memory_recall
    "compaction": {"tier", "before_tokens", "after_tokens"} | None,  # #4 用
    
    # —— M5-2 evaluator 新增(给 #3)——
    "chunk_usefulness": [
        {"role": str, "tokens": int, "useful_score": float},  # 0/0.5/1
        ...
    ],
    
    # —— 旧字段降级为 trace-only,不进顶层卡 ——
    "f1": float,
    "semantic_f1": float | None,
    "quality": float | None,
    "trace_payload": {...},
    "status": str,
    "error": str | None,
}
```

### 3.4 judge 调用编排(`metrics.run_judge`)

```python
async def run_judge(
    results: list[dict],
    qas: list,
    conversations: list,
    judge_llm,                  # None / LLMClient / async fn
    cache_path: Path | None,
    dataset_sha: str,           # sha256(locomo10.json)[:8]
) -> dict:
    """返回 5-key dict;失败 'uncomputed' / None。
    
    cache_path=None → 跑 judge 但不写盘;cache_path 给定 → 命中复用,未命中落盘。
    """
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
        return {**out, **json.loads(cache_file.read_text(encoding="utf-8"))}
    
    judged = {
        "1_recall":      await compute_recall(results, qas, conversations, judge_llm),
        "5_consistency": await compute_consistency(results, judge_llm),
    }
    if cache_file is not None:
        cache_file.write_text(
            json.dumps(judged, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return {**out, **judged}
```

**Judge prompt**(占位,plan 阶段定型):

```python
# #1 recall relevance
JUDGE_RECALL = (
    '判断记忆是否覆盖该 gold evidence(同事实/同实体即算覆盖)。\n'
    '只返 JSON {"relevant": bool}。'
)

# #5 entity extraction
JUDGE_ENTITIES = (
    '从 gold answer 抽取 key entities(人物/事件/物品/数字)。\n'
    '返 JSON {"entities": [str, ...]}。'
)

# #5 group consistency
JUDGE_GROUP_CONSIST = (
    '同一 entity 的多个 predicted answer 是否互相一致(同事实/同对象,允许近义)。\n'
    '返 JSON {"consistent": bool, "reason": str}。'
)

# #3 chunk usefulness(evaluator 层)
JUDGE_CHUNK = (
    '这段 context(对话历史/recall 结果)对该 QA 的回答是否有贡献?\n'
    '返 JSON {"useful": "yes"|"minor"|"no"}。'
)
```

### 3.5 report.py 顶层结构

```
┌───────────────────────────────────────────┐
│ Locomo Eval Report — M5-2 metrics v3      │
│ Samples 10 · QA 1986 · Time 4h12m · $0.83 │
└───────────────────────────────────────────┘

─── 5 张 metric 卡(grid)───
┌ #1 记忆召回 ┐ ┌ #2 时效性 ┐ ┌ #3 利用率 ┐
│ P 0.74     │ │ n 96      │ │ avg 0.31  │
│ R 0.62     │ │ pass 0.78 │ │ p50 0.27  │
│ n_e 384    │ │ f1 0.62   │ │ p90 0.58  │
└────────────┘ └───────────┘ └───────────┘
┌ #4 压缩率 ┐ ┌ #5 一致性 ┐
│ tier 1-3  │ │ groups 47 │
│ (详见 sub)│ │ drift 0.13│
└──────────┘ └──────────┘

─── 5 axis sub-tables(默认展开)───
[1. recall:    per-sample P/R 表]
[2. timeliness: 1 行(因数据少)]
[3. utilization: per-sample avg/p50/p90 + bucket 直方图]
[4. compaction:  per-tier 表]
[5. consistency: per-sample drift 表]

─── raw per-record table(<details> 默认折叠)───
sample_id · question · predicted · gold · pass · f1 · semantic · quality · tools · ...
```

**新 report.py 函数清单**:

| 函数 | 角色 |
|---|---|
| `_header(samples, qas, time, cost)` | 顶部 meta |
| `_summary_cards_v3(metrics) -> str` | 5 张卡(替换旧) |
| `_recall_subtable(metrics["1_recall"])` | per-sample 表 |
| `_timeliness_subtable(metrics["2_timeliness"])` | 单行表 |
| `_utilization_subtable(metrics["3_utilization"])` | per-sample + bucket 直方图 |
| `_compaction_subtable_v2(metrics["4_compaction"])` | per-tier 表 |
| `_consistency_subtable(metrics["5_consistency"])` | per-sample 表 |
| `_raw_records_table(results)` | 折叠 raw 表 |
| `write_html_report(out_path, results, metrics, ...)` | 主入口 |

**`uncomputed` / `None` 渲染为 `-`**。

---

## 4. 接口与数据格式

### 4.1 evaluator 接口(终态)

```python
async def evaluate_qa(
    prompt: str,
    predicted: str,
    gold: str,
    *,
    messages: list[dict] | None = None,     # M5-2 新增,默认 None 走旧路径
    judge_llm=None,
) -> dict:
    """Returns:
    {
      "f1": float,
      "semantic_f1": float | None,
      "quality": float | None,
      "pass": bool,                          # semantic_f1 > 0.7 OR f1 > 0.5(不变)
      "chunk_usefulness": [...],             # M5-2: messages 给 → 抽 chunk + judge;不给 → []
      "trace_payload": {...},
    }
    """
```

### 4.2 metrics 接口

```python
def compute_recall(results, qas, conversations, judge_llm) -> dict: ...
def compute_timeliness(results) -> dict: ...
def compute_utilization(results) -> dict: ...       # 无 judge,纯聚合
def compute_compaction_v2(results) -> dict: ...     # 无 judge,纯聚合
def compute_consistency(results, judge_llm) -> dict: ...
async def run_judge(results, qas, conversations, judge_llm, cache_path, dataset_sha) -> dict: ...

# 兼容旧 API(下一个版本删除):
def compute_by_q_type(results) -> dict: ...  # 保留,不在新 run_judge 调用
def compute_memory(results, qas, judge_llm) -> dict: ...  # 保留,不入新 run_judge
def compute_tool_accuracy(results, contexts, judge_llm) -> dict: ...  # 保留
```

### 4.3 dataset 接口(新增)

```python
def build_session_index(conversation: dict) -> dict[str, str]:
    """证据引用(D1:3 / D2:12)→ 所在 session_name。
    
    conversation 含 session_1_date_time / session_1 ... session_35_date_time。
    返回:{'D1:3': 'session_5', ...},只含 D1/D2 系列。
    
    走 utterances 累计偏移:每条 session_X 内的 dia_id 序号从 1 起。
    'D1:3' 意为 speaker D1(= speaker_a)的第 3 条 utterance,找它实际落在哪个 session_X。
    """
```

### 4.4 runner CLI(M5-1 不变)

```bash
python eval/locomo/runner.py                          # 走 metrics_v3:true 路径
python eval/locomo/runner.py --no-trace --limit 1    # smoke
python eval/locomo/runner.py --resume                 # 断点续
python eval/locomo/runner.py --no-memory-tools        # baseline
# 切回旧版:policy_local.yaml: metrics_v3: false
```

### 4.5 policy_local.yaml(M5-2 新增)

```yaml
locomo_eval:
  enabled: true
  trace_to_langfuse: true
  max_turns_per_sample: 500
  sample_timeout_s: 1800
  inject_memory_tools: true
  clear_memory_tags: ["locomo/"]
  metrics_v3: true             # NEW:false → runner 旧报告路径,True → M5-2 新指标
  judge_chunk_usefulness: true # NEW:false → evaluator 不跑 chunk judge,#3 = 'uncomputed'
```

---

## 5. 失败处理、kill-switch、回滚

### 5.1 失败模式

| 失败 | 处理 | 受影响 metric |
|---|---|---|
| `judge_llm is None` | #1, #5 → `"uncomputed"`;其它纯聚合仍跑 | #1, #5 |
| judge LLM API fail / 返非 JSON | per-pair try/except skip,fail-soft | #1, #5 |
| `chunk_usefulness` 全空 | #3 → `"uncomputed"` | #3 |
| `compaction` 全 None | #4 by_tier 全 0;overall=None | #4 |
| Temporal(category=3) 0 条 | #2 → 全字段 None | #2 |
| 0 个 memory_recall 调用 | 该 QA 在 #1 不计入 n_eligible | #1 |
| Evidence 全跨 session | 不计入 n_eligible | #1 |
| evaluator judge 异常(per chunk) | skip 该 chunk,空列表下#3 → 'uncomputed' | #3 |

### 5.2 kill-switch(双轨)

`policy_local.yaml: metrics_v3: false` 走**旧报告**:
- runner 调旧 `compute_by_q_type` / `compute_memory` / `compute_tool_accuracy`
- `evaluate_qa` **不传 messages**(走 M5-1 旧路径)
- `report.write_html_report` 检测 `metrics` 不含新 keys → 走 `_summary_cards`(旧版)

`metrics_v3: true` 走**新报告**(M5-2)。

### 5.3 缓存

- 路径:`<root>/.report-cache/locomo-judge-{dataset_sha8}.json`
- `dataset_sha` = `sha256(locomo10.json)[:8]`(路径同 .report-cache/locomo_session_index.json 一并)
- **不**跨数据集缓存(防 dataset 改了旧 key 误用)
- locomo runner 单进程串行,**不需要**并发锁

---

## 6. 测试

### 6.1 新测试(13 件)

**`tests/test_metrics_v3.py`**(9 件):

| # | 测什么 | fixture |
|---|---|---|
| 1 | `compute_timeliness` n=0 → 全 None | 空 results |
| 2 | `compute_timeliness` 4 category=3,3 pass → pass_rate=0.75,f1_med 中位数 | mock |
| 3 | `compute_utilization` weighted useful / total 计算 | 2 records × 3 chunks,score=1/0.5/0 |
| 4 | `compute_utilization` chunk_usefulness 全空 → 'uncomputed' | chunk=[] |
| 5 | `compute_compaction_v2` 全 None → by_tier 全 0,overall=None | mock |
| 6 | `compute_compaction_v2` per-tier 分类,avg_retain,pass_rate | 6 records 各 1 tier |
| 7 | `compute_recall` judge_llm=None → 'uncomputed' | mock results + qas |
| 8 | `compute_consistency` judge_llm=None → 'uncomputed' | 同上 |
| 9 | `compute_consistency` 同 sample 按 entity 分组正确 | gold 含 "speaker_a" 跨 3 record |

**`tests/test_evaluator_v3.py`**(4 件):

| # | 测什么 |
|---|---|
| 10 | `evaluate_qa(q,p,g,messages=msgs)` 返 `chunk_usefulness` 字段 |
| 11 | `evaluate_qa(q,p,g)` 不传 messages → `chunk_usefulness=[]` |
| 12 | `pass = semantic_f1 > 0.7 OR f1 > 0.5` 回归 |
| 13 | `_tokenize` 中文 token 切分回归 |

### 6.2 更新(2 处)

**`tests/test_report.py`**:
- `_summary_cards_v3` 渲染 5 卡(uncomputed 显 `-`)
- raw table `<details>` 默认折叠
- `metrics_v3: false` 路径走旧 cards(回归)

**`tests/test_metrics.py`**(M5-1 已有):
- `compute_by_q_type` / `compute_memory` / `compute_tool_accuracy` 保留为兼容测试,**继续通过**

### 6.3 数据 fixtures

- `data/locomo10.json`(M5-1 已用,**不动**)
- `.report-cache/locomo_session_index.json`:**首次 runner 自动生成**,跟 cache 同生命周期,gitignore

---

## 7. 时间表(Phase 拆分)

| Phase | 内容 | 估时 |
|---|---|---|
| P1 | `dataset.build_session_index` + `compute_recall` / `_timeliness` / `_utilization` / `_compaction_v2` / `_consistency` + `tests/test_metrics_v3.py` 9 个 | 1d |
| P2 | `evaluator.py` 加 `messages=` kwarg + `judge_chunk_usefulness` + new prompt + `tests/test_evaluator_v3.py` 4 个 | 0.5d |
| P3 | `report.py` 重写:`_summary_cards_v3` + 5 sub-table 函数 + raw 折叠 + `tests/test_report.py` 更新 | 1d |
| P4 | `runner.py` 改 1 行(messages=) + `policy_local.yaml` 加 2 keys | 0.25d |
| P5 | 全量 10 样本回放:`metrics_v3: true` 跑一次 → `false` 跑一次 → diff(确认旧路径仍可用) | 0.75d |
| **合计** | | **3.5d** |

---

## 8. 依赖与配置

**无新增依赖**。`_judge` 复用 M5-1 已写;`cc_harness.llm.LLMClient` 已存在;`datasets` json 解析用 `json`(stdlib)。

**配置文件**:`policy_local.yaml` 加 2 keys(§4.5);`.env` 不动;`pyproject.toml` 不动。

---

## 9. 风险 & 缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| cache 在数据集版本变更时复用旧结果 | 中 | 缓存文件路径含 `dataset_sha`(`sha256(locomo10.json)[:8]`),改了自动失效 |
| chunk judge LLM 调用 × QAs × n_chunks 过大(1986 × ~20 chunk = 40k 调用) | 中 | evaluator 跑 chunk judge 前先抽 cache:`(sample_id, role, sha256(content)[:16])` 已判过的跳过(同 conv 内对话历史大量重复) |
| judge 1000+ 次调用致 runner 时间翻倍 | 中 | judge 与 record 跑同步顺序,**不并发**(LLM 不友好);judge 用小模型(沿用 `OPENAI_MODEL`) |
| Temporal(category=3) 96 条样本小,统计不稳 | 低 | #2 sub-table 加 95% Wilson CI(bootstrap n=1000),plan 阶段定性 |
| evaluate_qa 加 kwarg 破坏旧测试 | 低 | `messages=None` 默认走旧路径,旧测试 100% 兼容 |
| 5 个 judge prompt 措辞产生 rubric drift | 低 | 所有 judge prompt 集中在 `metrics.py` 顶部常量 `JUDGE_*`,plan 写后统一复审 |
| 双轨并存复杂度 | 中 | `metrics_v3: false` 路径在 P5 diff 验证,稳定后下一个版本再下线 |

---

## 10. 关键设计决策(为什么)

- **5 维而非更多/更少**:用户原始列表,直接对齐;不擅自加 owner / 不擅自合并。
- **不构造 fact-correction 对**:数据集改造 ≠ 评测范围改造;要走会再开 spec。Temporal 96 条作为代理足够观察信号。
- **per-record chunk_usefulness**:在 evaluator.py 算,不污染 runner,让 judge 调用按 chunk 自然 dedup(同 conv 内对话历史大量重复)。
- **f1/semantic_f1/quality 降级为 trace-only**:符合 "现在只使用以下指标";trace 调试时仍可看。**双轨版本**给一个缓冲区。
- **`metrics_v3` 双轨并存**:M5-1 spec §5.2 留过 kill-switch;`metrics_v3: false` 是该开关的扩展,避免一刀切。
- **judge prompt 集中在 metrics.py**:5+1 个 prompt 字符串集中常量,plan 阶段一个 PR 一起审完。
- **per-tier 而非最严压缩档**:#4 用户语义是"看压缩后还能不能对",per-tier 看 "哪个 tier 起开始掉" 是最直接的答案。
- **不加新依赖**:M5-1 spec 已承诺最小依赖面;在此维持。
- **不做时间序列图**:plot.js 重依赖,just say no;数据在 JSON 里,后续要可视可外接 notebook。

---

## 11. 跟 M5-1 差异(给 reviewer)

| 段 | M5-1 (2026-07-07) | M5-2 (本 spec) | 为什么 |
|---|---|---|---|
| §1.1 GOAL | 跑通 + trace | 指标体系重排 | 用户新需求:5 维度替代旧 5 维度 |
| §1.3 OUT | (空) | 新增「不改数据集」「不加依赖」 | 收口 M5-2 不动的东西 |
| §2 per-record schema | `{f1, semantic_f1, quality, pass, trace_payload}` | 同 + `chunk_usefulness`,`pass` 不变 | 给 #3 的输入 |
| §3.2 模块清单 | 5 个新文件 | 不变 + `evaluator`/`metrics`/`report` 改;`runner` 1 行;`dataset` 加 1 函数 | 收敛到 metric 层重构 |
| §3.3 metrics API | by_q_type / compaction / utilization / token_series / memory / tool_accuracy | 5 个新 compute_* + run_judge 重写 | 完全替换 |
| §4.1 evaluator 签名 | `(prompt, predicted, gold, judge_llm=None)` | 加 `messages=None` kwarg | 给 #3 |
| §4.5 policy_local | 8 keys | + `metrics_v3` + `judge_chunk_usefulness` | 双轨开关 |
| §6.1 测试矩阵 | 3 件 | 9 + 4 = 13 新件 + 2 件回归 | 5 维各覆盖 |
| §5.2 kill-switch | 仅 `enabled`/`trace_to_langfuse`/... | 新增 `metrics_v3` 双轨 | 回滚路径 |

---

## 12. 文档交叉引用

- `docs/superpowers/specs/2026-07-07-locomo-eval-design.md` — 上承(M5-1,基础设施)
- `eval/locomo/runner.py:283-300` — per-record schema 现状(本 spec §3.3 对齐)
- `eval/locomo/evaluator.py:evaluate_qa` — 当前签名(本 spec §4.1 扩展)
- `eval/locomo/metrics.py:_judge` — judge 调用器(本 spec §3.4 复用)
- `docs/superpowers/plans/2026-07-20-locomo-metrics-v3.md` — (待写)实现计划
- CLAUDE.md §"Test conventions" — pytest 标记与 integration test 规则

---

## 13. 后续(spec 之外)

- 如要把 #2 时效性做严,需要构造 fact-correction 对 / 引外部数据集(开新 spec)。
- 如要把 #5 一致性更细,可在 entity 之外加"时间一致"维度(同 entity 在不同时间窗口出现)。
- judge prompt 的 rubric drift 监控(可在 P5 加一个 eval/bug/ 下做 5% sanity 抽样,需要新 spec)。

## 14. 开工前最后确认

- [ ] AC1-AC6(§1.4)
- [ ] 双轨并存 1 个版本周期(§5.2)
- [ ] judge prompt 5 个常量集中在 metrics.py 顶部(§3.4)
- [ ] 全 13 个新测试通过(§6.1)
