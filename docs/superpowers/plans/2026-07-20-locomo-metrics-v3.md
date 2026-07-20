# Locomo Metrics v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `eval/locomo/` 的指标体系从旧的 5 维度(by_q_type / compaction / utilization / token_series / memory / tool_accuracy)重排为用户定义的 5 个轴(记忆召回准确率 / 时效性 / 上下文利用率 / 上下文压缩率 / 多轮一致性)。

**Architecture:** spec `docs/superpowers/specs/2026-07-20-locomo-metrics-v3-design.md` §3 — `evaluator` 加 `chunk_usefulness`,`metrics` 全量替换 5 个 aggregate + 重写 `run_judge`,`report` 顶层只出 5 张卡 + sub-table(原 raw 折叠),`runner` 改 1 行,`policy_local.yaml` 加 `metrics_v3` 双轨开关。

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, ruff;无新依赖。

## Global Constraints

- 0 new dependencies(`_judge` / `LLMClient` / `dataset.py` / `MemoryService` 等 M5-1 已存在的工具一律复用)。
- judge prompt 集中在 `metrics.py` 顶部 4 个常量(`JUDGE_RECALL` / `JUDGE_ENTITIES` / `JUDGE_GROUP_CONSIST` / `JUDGE_CHUNK`)。
- 缓存路径 `<root>/.report-cache/locomo-judge-{dataset_sha8}.json`(由 runner 注入 `dataset_sha = sha256(locomo10.json)[:8]`)。
- 失败语义:`uncomputed` 表 judge 不可用 / `None` 表数据不可得,**绝不**抛异常出 aggregate。
- 双轨:`policy_local.yaml: metrics_v3: false` 走 M5-1 旧 `compute_by_q_type` / `compute_memory` / `compute_tool_accuracy`;`true` 走 M5-2 新 5 轴。M5-1 旧 API 函数保留作兼容,本版本不下线。
- `evaluate_qa` 加 `messages=None` kwarg(默认走旧路径,新 path 才填 `chunk_usefulness`)。
- 报告 `uncomputed` / `None` 渲染为 `-`,raw per-record 默认 `<details>` 折叠。
- 调用 git 用 `git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name=Claude Fable 5` 注入提交者身份。

**Windows 入口**:`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe`,避免 GBK 报错。

---

## Task 1: `dataset.build_session_index` 函数

**Files:**
- Modify: `eval/locomo/dataset.py`(加 1 函数)
- Create: `eval/locomo/tests/test_dataset_session_index.py`

**Interfaces:**
- Consumes: `conversation: dict`(LoCoMo JSON 节点,含 `session_1_date_time` / `session_1` / ... / `speaker_a` / `speaker_b`)
- Produces: `build_session_index(conversation: dict) -> dict[str, str]`(`{"D1:3": "session_5", ...}`)

> LoCoMo `session_X_date_time` 与 `session_X` 配对;`session_X` 是 `[{"speaker": "D1|D2", "dia_id": int, "text": str}, ...]`。`D1` 通常是 `speaker_a`,`D2` 是 `speaker_b`。要 map `"D1:3"`(speaker D1 第 3 条 utterance) → 它实际所在 session。

### Step 1: 写失败测试

在 `eval/locomo/tests/test_dataset_session_index.py`:

```python
"""build_session_index — 把 D1:3 / D2:5 这种 evidence ref 映射回 session_name。"""
from eval.locomo import dataset as ds


SAMPLE_CONV = {
    "speaker_a": "D1",
    "speaker_b": "D2",
    "session_1_date_time": "2024-01-01T10:00:00",
    "session_1": [
        {"speaker": "D1", "dia_id": 1, "text": "hi"},
        {"speaker": "D2", "dia_id": 2, "text": "hello"},
    ],
    "session_2_date_time": "2024-01-02T10:00:00",
    "session_2": [
        {"speaker": "D1", "dia_id": 3, "text": "how are you"},
        {"speaker": "D2", "dia_id": 4, "text": "fine"},
    ],
    "session_3_date_time": "2024-01-03T10:00:00",
    "session_3": [
        {"speaker": "D1", "dia_id": 5, "text": "bye"},
    ],
}


def test_first_session_first_utterance():
    idx = ds.build_session_index(SAMPLE_CONV)
    assert idx["D1:1"] == "session_1"
    assert idx["D2:2"] == "session_1"


def test_cross_session_reference():
    idx = ds.build_session_index(SAMPLE_CONV)
    # D1:3 在 session_2 里,不在 session_1
    assert idx["D1:3"] == "session_2"
    # D2:4 在 session_2
    assert idx["D2:4"] == "session_2"


def test_last_session_last_utterance():
    idx = ds.build_session_index(SAMPLE_CONV)
    assert idx["D1:5"] == "session_3"


def test_real_locomo_conversation_runs():
    """用真实 locomo10.json 的第一个对话跑一遍,build 不抛。"""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    data_file = repo / "eval/locomo/data/locomo10.json"
    if not data_file.exists():
        import pytest
        pytest.skip("locomo10.json missing; run download_dataset.py first")
    import json
    conv = json.loads(data_file.read_text(encoding="utf-8"))[0]["conversation"]
    idx = ds.build_session_index(conv)
    assert isinstance(idx, dict)
    # 至少覆盖 D1:1 / D1:2 / D1:3
    assert any(k.startswith("D1:") for k in idx.keys())
    assert any(k.startswith("D2:") for k in idx.keys())
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_dataset_session_index.py -v
```

Expected: FAIL with `AttributeError: module 'eval.locomo.dataset' has no attribute 'build_session_index'`

### Step 3: 实现 `build_session_index`

在 `eval/locomo/dataset.py` 末尾追加(其它已有函数保留不动):

```python
def build_session_index(conversation: dict) -> dict[str, str]:
    """证据引用(D1:3 / D2:12)→ 所在 session_name。

    conversation 含 session_1_date_time / session_1 ... session_N_date_time / session_N
    及顶层 speaker_a / speaker_b (值是 'D1' / 'D2')。

    返回 {'D1:3': 'session_5', ...},只覆盖 D1 / D2 系列 refs。

    算法:
      1. 抽取 N(有多少个 session_*_date_time)
      2. 对每个 session_X(按 X 数值排),取 conversation[f'session_{X}']
      3. 对该 session 内每条 utterance:
         - 'dia_id' 为 1-based 编号;但不严格自增(可能跨 session 累加)
         - 直接用 (speaker, dia_id) → session_name
    """
    out: dict[str, str] = {}
    session_keys = sorted(
        (k for k in conversation.keys() if k.startswith("session_") and k.endswith("_date_time")),
        key=lambda k: int(k[len("session_"):-len("_date_time")]),
    )
    for sk_date in session_keys:
        n = sk_date[len("session_"):-len("_date_time")]
        sk = f"session_{n}"
        for utt in conversation.get(sk, []):
            speaker = utt["speaker"]
            dia_id = utt["dia_id"]
            out[f"{speaker}:{dia_id}"] = sk
    return out
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_dataset_session_index.py -v
```

Expected: PASS,4 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/dataset.py eval/locomo/tests/test_dataset_session_index.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo): dataset.build_session_index — evidence ref → session_name

M5-2 指标 1 前置。LoCoMo qa.evidence 字段是 'D1:3' 这种引用,
需要预先映射回它所在的 session,后续 compute_recall 才能筛
"evidence 全在同一 session 的 QA"。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: `metrics.compute_timeliness`

**Files:**
- Modify: `eval/locomo/metrics.py`(新增 1 函数)
- Modify: `eval/locomo/tests/test_metrics.py`(追加 test)

**Interfaces:**
- Consumes: `results: list[dict]`,每个 record 含 `q_type`(LoCoMo category,str)与 `pass`/`f1`/`semantic_f1`
- Produces: `compute_timeliness(results: list[dict]) -> dict`(纯聚合,**无 judge**)

> 99% 是 `category=3`(Temporal)的子集;spec §2.2 写明 n=0 时全 `None`,但要返回 dict 不是 raise。

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_metrics.py`:

```python
def test_compute_timeliness_basic():
    from eval.locomo import metrics
    results = [
        {"q_type": "3", "pass": True, "f1": 0.8, "semantic_f1": 0.9},
        {"q_type": "3", "pass": True, "f1": 0.6, "semantic_f1": 0.7},
        {"q_type": "3", "pass": False, "f1": 0.2, "semantic_f1": 0.1},
        {"q_type": "3", "pass": True, "f1": 0.5, "semantic_f1": 0.6},
        {"q_type": "1", "pass": True, "f1": 1.0, "semantic_f1": 1.0},  # 排除
    ]
    out = metrics.compute_timeliness(results)
    assert out["n"] == 4
    assert out["pass_rate"] == 0.75
    # f1 sorted: [0.2, 0.5, 0.6, 0.8], median = (0.5+0.6)/2 = 0.55
    assert abs(out["f1_med"] - 0.55) < 1e-6
    assert abs(out["semantic_f1_med"] - 0.65) < 1e-6


def test_compute_timeliness_empty():
    from eval.locomo import metrics
    out = metrics.compute_timeliness([])
    assert out["n"] == 0
    assert out["pass_rate"] is None
    assert out["f1_med"] is None


def test_compute_timeliness_no_temporal():
    from eval.locomo import metrics
    results = [{"q_type": "1", "pass": True, "f1": 0.5, "semantic_f1": 0.6}]
    out = metrics.compute_timeliness(results)
    assert out["n"] == 0
    assert out["pass_rate"] is None
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py::test_compute_timeliness_basic -v
```

Expected: FAIL `AttributeError: module 'eval.locomo.metrics' has no attribute 'compute_timeliness'`

### Step 3: 实现 `compute_timeliness`

在 `eval/locomo/metrics.py` 顶部 docstring 后面追加(已有 `_judge` 等函数保留):

```python
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
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v -k timeliness
```

Expected: PASS,3 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): metrics.compute_timeliness — Temporal 子集纯聚合

M5-2 指标 2。复用 statistics.median,fail-soft n=0 → 全 None。
不引 judge(纯聚合指标)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: `metrics.compute_utilization`

**Files:**
- Modify: `eval/locomo/metrics.py`(加 1 函数)
- Modify: `eval/locomo/tests/test_metrics.py`(追加 test)

**Interfaces:**
- Consumes: `results: list[dict]`,每 record 含 `chunk_usefulness: [{role, tokens, useful_score}]` 与 `prompt_tokens`
- Produces: `compute_utilization(results: list[dict]) -> dict`(纯聚合,**无 judge**)

> ratio = sum(score * tokens) / prompt_tokens
> 不挑 chunk 类型:全部 chunk(系统 / user / tool)都参与 judge,见 §2.3 spec

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_metrics.py`:

```python
def test_compute_utilization_basic():
    from eval.locomo import metrics
    results = [
        {
            "prompt_tokens": 1000,
            "chunk_usefulness": [
                {"role": "system", "tokens": 200, "useful_score": 1.0},   # 200
                {"role": "user",   "tokens": 500, "useful_score": 0.5},   # 250
                {"role": "tool",   "tokens": 300, "useful_score": 0.0},   # 0
            ],
            # weighted useful = 200 + 250 + 0 = 450; ratio = 0.45
        },
        {
            "prompt_tokens": 800,
            "chunk_usefulness": [
                {"role": "system", "tokens": 100, "useful_score": 1.0},   # 100
                {"role": "user",   "tokens": 700, "useful_score": 1.0},   # 700
            ],
            # ratio = 1.0
        },
    ]
    out = metrics.compute_utilization(results)
    assert out["n"] == 2
    assert abs(out["avg"] - 0.725) < 1e-6    # (0.45 + 1.0) / 2
    # 排序 [0.45, 1.0], p50 = 0.725(p50 in this context uses median — OK to match avg here by chance)


def test_compute_utilization_missing_chunks():
    from eval.locomo import metrics
    results = [
        {"prompt_tokens": 1000, "chunk_usefulness": []},  # 全空
        {"prompt_tokens": 800,  "chunk_usefulness": []},  # 全空
    ]
    out = metrics.compute_utilization(results)
    assert out == "uncomputed"


def test_compute_utilization_partial_chunks():
    """n_chunks 全空之一记录被忽略,只算有 chunk 的。"""
    from eval.locomo import metrics
    results = [
        {"prompt_tokens": 1000, "chunk_usefulness": []},  # skip
        {"prompt_tokens": 500, "chunk_usefulness": [
            {"role": "system", "tokens": 500, "useful_score": 1.0},
        ]},  # ratio = 1.0
    ]
    out = metrics.compute_utilization(results)
    assert out["n"] == 1
    assert out["avg"] == 1.0
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py::test_compute_utilization_basic -v
```

Expected: FAIL `AttributeError: ... has no attribute 'compute_utilization'`

### Step 3: 实现 `compute_utilization`

在 `metrics.py` 追加:

```python
def compute_utilization(results: list[dict]) -> dict:
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
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v -k utilization
```

Expected: PASS,3 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): metrics.compute_utilization — useful_token / prompt_token

M5-2 指标 3 纯聚合端。chunk_usefulness 全空 → 'uncomputed'。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: `metrics.compute_compaction_v2`

**Files:**
- Modify: `eval/locomo/metrics.py`(加 1 函数,已有 `compute_compaction` 保留作兼容)
- Modify: `eval/locomo/tests/test_metrics.py`(追加 test)

**Interfaces:**
- Consumes: `results: list[dict]`,每个 record 含 `compaction: {tier, before_tokens, after_tokens} | None`
- Produces: `compute_compaction_v2(results: list[dict]) -> dict`(per-tier 桶 + 整体)

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_metrics.py`:

```python
def test_compute_compaction_v2_no_compaction():
    """所有 record.compaction=None → by_tier 全 0;overall_avg_retain=None。"""
    from eval.locomo import metrics
    results = [
        {"compaction": None, "pass": True},
        {"compaction": None, "pass": False},
    ]
    out = metrics.compute_compaction_v2(results)
    assert out["total_compressed_n"] == 0
    assert out["overall_avg_retain"] is None
    for row in out["by_tier"]:
        assert row["trigger_n"] == 0
    by_tier_map = {r["tier"]: r for r in out["by_tier"]}
    assert by_tier_map[0]["pass_rate"] == 0.5   # 1 pass / 2 records


def test_compute_compaction_v2_per_tier():
    from eval.locomo import metrics
    results = [
        # tier 0
        {"compaction": None, "pass": True},
        # tier 1:avg retain = (0.8 + 0.6) / 2 = 0.7;1 pass,1 fail
        {"compaction": {"tier": 1, "before_tokens": 1000, "after_tokens": 800}, "pass": True},
        {"compaction": {"tier": 1, "before_tokens": 1000, "after_tokens": 600}, "pass": False},
        # tier 2:retain = 0.5;pass True
        {"compaction": {"tier": 2, "before_tokens": 800, "after_tokens": 400}, "pass": True},
        # tier 3:retain = 0.2;pass False(失分)
        {"compaction": {"tier": 3, "before_tokens": 500, "after_tokens": 100}, "pass": False},
    ]
    out = metrics.compute_compaction_v2(results)
    by_tier_map = {r["tier"]: r for r in out["by_tier"]}
    assert by_tier_map[0]["trigger_n"] == 1
    assert by_tier_map[1]["trigger_n"] == 2
    assert abs(by_tier_map[1]["avg_retain"] - 0.7) < 1e-6
    assert by_tier_map[1]["pass_rate"] == 0.5
    assert by_tier_map[2]["trigger_n"] == 1
    assert by_tier_map[2]["avg_retain"] == 0.5
    assert by_tier_map[3]["trigger_n"] == 1
    assert by_tier_map[3]["avg_retain"] == 0.2
    assert by_tier_map[3]["pass_rate"] == 0.0
    assert out["total_compressed_n"] == 4
    assert abs(out["overall_avg_retain"] - (0.7 + 0.5 + 0.2) / 3) < 1e-6


def test_compute_compaction_v2_partial_retain():
    """before/after 缺失 → 该 record 不计入 avg_retain,但计入 trigger_n 与 pass_rate。"""
    from eval.locomo import metrics
    results = [
        {"compaction": {"tier": 1, "before_tokens": None, "after_tokens": None}, "pass": True},
        {"compaction": {"tier": 1, "before_tokens": 1000, "after_tokens": 500}, "pass": True},
    ]
    out = metrics.compute_compaction_v2(results)
    by_tier_map = {r["tier"]: r for r in out["by_tier"]}
    assert by_tier_map[1]["trigger_n"] == 2
    assert by_tier_map[1]["pass_rate"] == 1.0
    assert abs(by_tier_map[1]["avg_retain"] - 0.5) < 1e-6   # 仅第二条计入
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py::test_compute_compaction_v2_no_compaction -v
```

Expected: FAIL `AttributeError: ... has no attribute 'compute_compaction_v2'`

### Step 3: 实现 `compute_compaction_v2`

```python
def compute_compaction_v2(results: list[dict]) -> dict:
    """#4 上下文压缩率:per-tier 分桶 + 整体 avg_retain。

    tier=0 表示该 record 未触发压缩(compaction is None 或 tier=0);
    tier>=1 表示压缩过。
    """
    by_tier: dict[int, dict] = {t: {"tier": t, "trigger_n": 0, "pass": 0,
                                     "retain_sum": 0.0, "retain_count": 0}
                                  for t in (0, 1, 2, 3)}
    total_compressed = 0
    retain_values: list[float] = []
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
            if tier >= 1:
                retain_values.append(ratio)
    by_tier_rows = []
    for t in (0, 1, 2, 3):
        row = by_tier[t]
        n = row["trigger_n"]
        by_tier_rows.append({
            "tier": t,
            "trigger_n": n,
            "avg_retain": (row["retain_sum"] / row["retain_count"]) if row["retain_count"] else None,
            "pass_rate": (row["pass"] / n) if n else None,
        })
    return {
        "by_tier": by_tier_rows,
        "total_compressed_n": total_compressed,
        "overall_avg_retain": st.mean(retain_values) if retain_values else None,
    }
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v -k compaction_v2
```

Expected: PASS,3 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): metrics.compute_compaction_v2 — per-tier + pass_rate 联合

M5-2 指标 4。复用 runner 已产的 record.compaction.{tier, before, after},
不引新数据源。'old' compute_compaction 保留作 M5-1 兼容。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: `metrics.compute_recall`(指标 1,judge 路径)

**Files:**
- Modify: `eval/locomo/metrics.py`(加 1 函数)
- Modify: `eval/locomo/tests/test_metrics.py`(追加 test,使用 FakeLLM)

**Interfaces:**
- Consumes: `results, qas, conversations, judge_llm`
  - `conversations`: **`Dict[str, conv_dict]`**(sample_id → conversation),非 list(plan 修订:list 形式 lookup bug-prone)
- Produces: `compute_recall(results, qas, conversations, judge_llm) -> dict`

> judge 形态:`LLMClient` 或 `async fn(str)->str` 或 `None`。`_judge`(`metrics.py:73`)已支持,直接复用。
> 单元测试用 `async fn` 形式 FakeLLM,无需 mock LLMClient。
> LoCoMo runner 调用:`{r["sample_id"]: conv for conv in conversations}` 一次构造 dict。

### Step 1: 顶部加 judge prompt 常量

在 `metrics.py` 顶部 docstring 之后:

```python
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
```

### Step 2: 写失败测试

追加到 `eval/locomo/tests/test_metrics.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_compute_recall_uncomputed_no_judge():
    from eval.locomo import metrics
    results = [{"tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC"}]}]
    qas = [{"evidence": ["D1:1"]}]
    conv = {"D1:1": "session_1"}
    out = await metrics.compute_recall(results, qas, None, judge_llm=None)
    assert out == "uncomputed"


@pytest.mark.asyncio
async def test_compute_recall_basic_precision_recall():
    """1 QA,2 evidences,evidence 全在同一 session(monkeypatch session index)。

    judge 总是返 {'relevant': True},expected:
      n_eligible = 1
      precision  = 1 recall return
      recall     = 2 evidence / 2 total = 1.0
    """
    from eval.locomo import metrics
    from eval.locomo import dataset as ds

    async def fake_judge(system, user):
        return '{"relevant": true}'

    conversations = {
        "conv-X": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [
                {"speaker": "D1", "dia_id": 1, "text": "Alice lives in NYC"},
                {"speaker": "D1", "dia_id": 2, "text": "Alice works at OpenAI"},
            ],
        }
    }
    results = [{
        "sample_id": "conv-X",
        "tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC works at OpenAI"}],
    }]
    qas = [{"evidence": ["D1:1", "D1:2"]}]  # 都在 session_1 → eligible
    out = await metrics.compute_recall(results, qas, conversations, judge_llm=fake_judge)
    assert out["n_eligible"] == 1
    assert out["n_total_recall"] == 1
    assert out["precision"] == 1.0
    assert out["recall"] == 1.0


@pytest.mark.asyncio
async def test_compute_recall_cross_session_excluded():
    """evidence 跨 ≥2 session → 该 QA 不算 n_eligible。

    模拟 judge 返 True(理应返 False),验证只看 n_eligible。
    """
    from eval.locomo import metrics

    async def fake_judge(system, user):
        return '{"relevant": true}'

    conv = {
        "conv-Y": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [{"speaker": "D1", "dia_id": 1, "text": "x"}],
            "session_2_date_time": "2024-01-02T10:00:00",
            "session_2": [{"speaker": "D1", "dia_id": 2, "text": "y"}],
        }
    }
    results = [{"sample_id": "conv-Y", "tool_calls": []}]
    qas = [{"evidence": ["D1:1", "D1:2"]}]  # 跨 session
    out = await metrics.compute_recall(results, qas, conv, judge_llm=fake_judge)
    assert out["n_eligible"] == 0
    assert out["precision"] is None
    assert out["recall"] is None
```

### Step 3: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py::test_compute_recall_uncomputed_no_judge -v
```

Expected: FAIL `AttributeError: ... has no attribute 'compute_recall'`

### Step 4: 实现 `compute_recall`

```python
async def compute_recall(results, qas, conversations, judge_llm) -> dict:
    """#1 记忆召回准确率。

    `conversations`: Dict[sample_id, conv_dict](非 list)。caller 负责构造,
    例如:{r["sample_id"]: conv for conv in conversations_list}。
    """
    if judge_llm is None:
        return "uncomputed"

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
        sessions = {idx.get(ev) for ev in evidence if ev in idx}
        sessions.discard(None)
        if not sessions or len(sessions) > 1:
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
                resp = await _judge(judge_llm, JUDGE_RECALL,
                                    f"记忆:\n{recall_text}\n\n证据:\n{ev}")
                if json.loads(resp).get("relevant"):
                    r_num += 1
                    p_num += 1
            except Exception:
                pass
            r_den += 1
        p_den += len(recall_calls)

    return {
        "n_eligible": n_eligible,
        "n_total_recall": n_total_recall,
        "precision": (p_num / p_den) if p_den else None,
        "recall":    (r_num / r_den) if r_den else None,
    }
```

`build_session_index` 来自 Task 1;`_judge` 来自 `metrics.py:73`。

### Step 5: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v -k recall
```

Expected: PASS,3 tests。

### Step 6: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): metrics.compute_recall — 单 session 证据 + judge P/R

M5-2 指标 1。仅算 evidence 全在同一 session 的 QA(metrics-pass);
跨 session QA 排除。结果含 n_eligible / precision / recall。
judge 不可用 → 'uncomputed';per-pair judge 异常 fail-soft skip。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: `metrics.compute_consistency`(指标 5)

**Files:**
- Modify: `eval/locomo/metrics.py`(加 1 函数)
- Modify: `eval/locomo/tests/test_metrics.py`(追加 test)

**Interfaces:**
- Consumes: `results: list[dict]`,`judge_llm`
- Produces: `compute_consistency(results, judge_llm) -> dict`

> 算法步骤(per spec §2.5):
> 1) `by_sample = groupby(results, sample_id)`
> 2) per record:`JUDGE_ENTITIES` → `[entity1, entity2, ...]`
> 3) per (sample, entity):收录所有 records 的 predicted → group
> 4) 仅保留 entity 出现 ≥2 records 的 group
> 5) per group:`JUDGE_GROUP_CONSIST` → `{consistent: bool, reason}`
> 6) drift_rate = (inconsistent groups) / (总 groups)

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_metrics.py`:

```python
@pytest.mark.asyncio
async def test_compute_consistency_uncomputed_no_judge():
    from eval.locomo import metrics
    out = await metrics.compute_consistency([], judge_llm=None)
    assert out == "uncomputed"


@pytest.mark.asyncio
async def test_compute_consistency_grouping():
    """1 conversation,3 records:gold 含 'speaker_a' 实体 2 次,另 1 个 entity 1 次。

    第一组('speaker_a', 出现 2 次)→ judge 期望返 consistent=True → drift 0。
    期望:drift_rate = 0,n_groups = 1。
    """
    from eval.locomo import metrics

    async def fake_judge(system, user):
        if "key entities" in system.lower() or "抽取" in system:
            # entity extraction call
            if "Alice" in user or "speaker_a" in user:
                return '{"entities": ["speaker_a"]}'
            return '{"entities": []}'
        # group consistency call
        return '{"consistent": true, "reason": "ok"}'

    results = [
        {"sample_id": "conv-X", "question": "q1", "gold": "speaker_a is an engineer",
         "predicted": "engineer"},
        {"sample_id": "conv-X", "question": "q2", "gold": "speaker_a lives in NYC",
         "predicted": "NYC"},
        {"sample_id": "conv-X", "question": "q3", "gold": "Alice is a teacher",
         "predicted": "no idea"},
    ]
    out = await metrics.compute_consistency(results, judge_llm=fake_judge)
    assert out["n_groups"] == 1
    assert out["drift_rate"] == 0.0


@pytest.mark.asyncio
async def test_compute_consistency_drift_detected():
    """同 entity 跨 2 records 但 predicted 冲突 → drift。"""
    from eval.locomo import metrics

    async def fake_judge(system, user):
        if "抽取" in system:
            return '{"entities": ["speaker_a"]}'
        return '{"consistent": false, "reason": "teacher vs engineer"}'

    results = [
        {"sample_id": "conv-Y", "question": "q1", "gold": "speaker_a is engineer", "predicted": "engineer"},
        {"sample_id": "conv-Y", "question": "q2", "gold": "speaker_a is engineer", "predicted": "teacher"},
    ]
    out = await metrics.compute_consistency(results, judge_llm=fake_judge)
    assert out["n_groups"] == 1
    assert out["drift_rate"] == 1.0   # 1 group, 1 drift
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py::test_compute_consistency_uncomputed_no_judge -v
```

Expected: FAIL `AttributeError: ... has no attribute 'compute_consistency'`

### Step 3: 实现 `compute_consistency`

```python
import hashlib


def _sample_key(record) -> str:
    return record.get("sample_id", "")


async def compute_consistency(results: list[dict], judge_llm) -> dict:
    """#5 多轮一致性:同 conversation 同 entity ≥2 records → judge 一致性。"""
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

    # 仅保留 ≥2 records 的 group
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
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v -k consistency
```

Expected: PASS,3 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): metrics.compute_consistency — 同 entity 反复出现 judge 一致性

M5-2 指标 5。per sample 按 entity(group 含 ≥2 records)聚合,
judge 评 predicted 是否互相一致 + vs gold。drift_rate = 冲突组 / 总组。
per-judge fail-soft skip。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: `metrics.run_judge` 编排重写

**Files:**
- Modify: `eval/locomo/metrics.py`(覆盖 `run_judge`)
- Modify: `eval/locomo/tests/test_metrics.py`(追加 test)

**Interfaces:**
- Consumes: `results, qas, conversations, judge_llm, cache_path, dataset_sha`
- Produces: `await run_judge(...) -> dict`(5-key)

> `run_judge` 旧实现(M5-1)在 `metrics.py:137`;新实现按 spec §3.4。

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_metrics.py`:

```python
@pytest.mark.asyncio
async def test_run_judge_no_judge_returns_5key():
    from eval.locomo import metrics
    results = [
        {"q_type": "3", "pass": True, "f1": 0.5, "semantic_f1": 0.6,
         "prompt_tokens": 100, "chunk_usefulness": [{"role":"system","tokens":50,"useful_score":1.0}],
         "compaction": None, "tool_calls": [], "sample_id": "x"},
    ]
    out = await metrics.run_judge(results, [], [], judge_llm=None,
                                   cache_path=None, dataset_sha="abc12345")
    assert set(out.keys()) == {"1_recall", "2_timeliness", "3_utilization", "4_compaction", "5_consistency"}
    assert out["1_recall"] == "uncomputed"
    assert out["5_consistency"] == "uncomputed"
    assert isinstance(out["2_timeliness"], dict)
    assert isinstance(out["3_utilization"], dict)  # chunk 给齐
    assert isinstance(out["4_compaction"], dict)


@pytest.mark.asyncio
async def test_run_judge_cache_hit():
    """cache_file 存在 → 复用 cache,不调 judge。"""
    from eval.locomo import metrics
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp)
        (cache_path / "locomo-judge-abc12345.json").write_text(
            json.dumps({"1_recall": {"n_eligible": 99, "precision": 0.5, "recall": 0.5},
                        "5_consistency": {"n_groups": 10, "drift_rate": 0.1, "by_sample": []}}),
            encoding="utf-8",
        )
        called = []
        async def fake_judge(*a, **kw):
            called.append(1)
            return "{}"

        out = await metrics.run_judge([], [], [], judge_llm=fake_judge,
                                       cache_path=cache_path, dataset_sha="abc12345")
        assert out["1_recall"]["n_eligible"] == 99  # 缓存命中
        assert out["5_consistency"]["n_groups"] == 10
        assert called == []  # 没调 judge


def test_run_judge_signature():
    """签名稳定:5 个 key 必须存在(防止未来 contract 回归)。"""
    import inspect
    from eval.locomo import metrics
    sig = inspect.signature(metrics.run_judge)
    params = list(sig.parameters.keys())
    for name in ("results", "qas", "conversations", "judge_llm", "cache_path", "dataset_sha"):
        assert name in params, f"missing param: {name}"
```

把 `from pathlib import Path` 加到 file 顶部如果还没有。

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py::test_run_judge_signature -v
```

Expected: PASS(可能因旧 `run_judge` 还在; 但 `test_run_judge_no_judge_returns_5key` 会 FAIL,因为 keys 不是 5 个)。

### Step 3: 改写 `run_judge`

替换 `metrics.py` 末尾 `run_judge`:

```python
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
```

M5-1 旧 `compute_by_q_type` / `compute_memory` / `compute_tool_accuracy` / 旧 `_run()` helper **保留**(供 `metrics_v3: false` 路径使用)。

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v -k run_judge
```

Expected: PASS,3 tests。

### Step 5: 跑全 metrics 测试,确认旧 API 仍兼容

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_metrics.py -v
```

Expected: ALL PASS(include legacy `compute_by_q_type` tests if any)。

### Step 6: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/metrics.py eval/locomo/tests/test_metrics.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): metrics.run_judge — 5-key 编排 + judge 缓存

返回 5 个 keys(1_recall/2_timeliness/3_utilization/4_compaction/5_consistency)。
judge_llm=None → 1/5 'uncomputed',其它纯聚合仍跑。
cache_path 命中复用,不命中写盘。judge 与 record 同步跑,不并发。
旧 M5-1 API 函数保留作双轨兼容。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: `evaluator.judge_chunk_usefulness`(指标 3 judge 端)

**Files:**
- Modify: `eval/locomo/evaluator.py`(加 1 函数,顶部常量)
- Create: `eval/locomo/tests/test_evaluator_v3.py`

**Interfaces:**
- Consumes: `chunk_content: str, qa_q: str, qa_gold: str, judge_llm`
- Produces: `await judge_chunk_usefulness(...) -> float`(`0.0` / `0.5` / `1.0`)

> 把 `JUDGE_CHUNK` 加到 `evaluator.py` 顶部常量,与 `metrics.py` JUDGE_* 风格一致。

### Step 1: 写失败测试

在 `eval/locomo/tests/test_evaluator_v3.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_yes():
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return '{"useful": "yes"}'

    score = await judge_chunk_usefulness(
        "Alice lives in NYC", "Where does Alice live?", "NYC",
        judge_llm=fake_judge,
    )
    assert score == 1.0


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_no():
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return '{"useful": "no"}'

    score = await judge_chunk_usefulness(
        "Bob likes pizza", "Where does Alice live?", "NYC",
        judge_llm=fake_judge,
    )
    assert score == 0.0


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_minor():
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return '{"useful": "minor"}'

    score = await judge_chunk_usefulness(
        "Alice visited many cities", "Where does Alice live?", "NYC",
        judge_llm=fake_judge,
    )
    assert score == 0.5


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_bad_json_returns_zero():
    """judge 返非 JSON → fail-soft 0.0。"""
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return "not json"

    score = await judge_chunk_usefulness("x", "q", "g", judge_llm=fake_judge)
    assert score == 0.0
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator_v3.py -v
```

Expected: FAIL `ImportError` / `AttributeError`。

### Step 3: 实现 `judge_chunk_usefulness`

在 `eval/locomo/evaluator.py` 顶部加常量:

```python
JUDGE_CHUNK = (
    '这段 context(对话历史 / recall 结果)对该 QA 的回答是否有贡献?\n'
    '返 JSON {"useful": "yes" | "minor" | "no"}。'
)
```

并在文件末尾追加:

```python
async def judge_chunk_usefulness(
    chunk_content: str,
    qa_q: str,
    qa_gold: str,
    judge_llm,
) -> float:
    """#3 context chunk 是否对最终 answer 有贡献(yes=1.0 / minor=0.5 / no=0.0)。"""
    if judge_llm is None:
        return 0.0
    user = f"chunk:\n{chunk_content}\n\nquestion: {qa_q}\n\ngold_answer: {qa_gold}"
    try:
        resp = await _judge(judge_llm, JUDGE_CHUNK, user)
        useful = json.loads(resp).get("useful", "no")
        return {"yes": 1.0, "minor": 0.5, "no": 0.0}.get(useful, 0.0)
    except Exception as e:
        logger.warning("judge_chunk_usefulness failed: %s", e)
        return 0.0
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator_v3.py -v
```

Expected: PASS,4 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/evaluator.py eval/locomo/tests/test_evaluator_v3.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): evaluator.judge_chunk_usefulness — yes/minor/no → 1/0.5/0

M5-2 指标 3 evaluator 端。judge 不可用 → 0;judge 异常 fail-soft 0。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: `evaluator.evaluate_qa` 扩展签名 + chunk_usefulness 端到端

**Files:**
- Modify: `eval/locomo/evaluator.py`
- Modify: `eval/locomo/tests/test_evaluator_v3.py`(追加)

**Interfaces:**
- Consumes: `messages: list[dict] | None`,`judge_llm`
- Produces: `evaluate_qa(...) -> dict` 多含 `chunk_usefulness` 字段

> `messages` 切 chunk 规则(spec §2.3):system 1 个、每 user 1 个、每 tool 1 个、assistant 跳过。
> chunk 缓存 key:`(sample_id, role, sha256(content)[:16])` —— 但 evaluator 不知 sample_id,简化用 `sha256(content)[:16]` 即可(同 content 不同 sample 极少见)。

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_evaluator_v3.py`:

```python
@pytest.mark.asyncio
async def test_evaluate_qa_chunk_usefulness_attached():
    from eval.locomo.evaluator import evaluate_qa

    async def fake_judge(system, user):
        return '{"useful": "yes"}'

    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "[Alice] hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "[Alice] where do I live?"},
        {"role": "tool", "content": "NYC"},
    ]
    out = await evaluate_qa(
        "where does Alice live?", "NYC", "NYC",
        messages=messages, judge_llm=fake_judge,
    )
    assert "chunk_usefulness" in out
    # assistant 跳过 → 4 个 chunk
    assert len(out["chunk_usefulness"]) == 4
    # 每条都 judge "yes" → 全 1.0
    assert all(c["useful_score"] == 1.0 for c in out["chunk_usefulness"])
    # role 顺序:system / user / user / tool
    assert [c["role"] for c in out["chunk_usefulness"]] == ["system", "user", "user", "tool"]


@pytest.mark.asyncio
async def test_evaluate_qa_no_messages_returns_empty_chunks():
    from eval.locomo.evaluator import evaluate_qa

    out = await evaluate_qa("q", "p", "g")  # 不传 messages
    assert "chunk_usefulness" in out
    assert out["chunk_usefulness"] == []


@pytest.mark.asyncio
async def test_evaluate_qa_pass_unchanged():
    """回归:pass = semantic_f1>0.7 OR f1>0.5。"""
    from eval.locomo.evaluator import evaluate_qa

    async def fake_judge(system, user):
        # semantic_f1 → 返 0.9,chunk_usefulness → yes
        if "语义等价" in system:
            return '{"score": 0.9}'
        return '{"useful": "yes"}'

    out = await evaluate_qa("q", "p", "g", judge_llm=fake_judge)
    assert out["pass"] is True
    assert out["semantic_f1"] == 0.9


@pytest.mark.asyncio
async def test_evaluate_qa_token_f1_cjk_unchanged():
    """回归:_tokenize CJK 不变。"""
    from eval.locomo.evaluator import evaluate_qa

    out = await evaluate_qa("问什么问题", "问什么问题", "问什么问题")  # 无 judge
    assert out["f1"] == 1.0
    # 无 judge_llm → chunk_usefulness = []
    assert out["chunk_usefulness"] == []
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator_v3.py::test_evaluate_qa_chunk_usefulness_attached -v
```

Expected: FAIL `TypeError: evaluate_qa() got an unexpected keyword argument 'messages'`(或 keys 缺)。

### Step 3: 改 `evaluate_qa`

在 `evaluator.py`:`evaluate_qa` 当前签名(line 105):

```python
async def evaluate_qa(prompt, predicted, gold, judge_llm=None) -> dict:
```

改为:

```python
def _chunk_messages(messages: list[dict]) -> list[dict]:
    """messages → chunks(list[{role, content, tokens}])。assistant 跳过。"""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            # OpenAI 多模态格式(本 runner 不会出,容错)
            content = json.dumps(content, ensure_ascii=False)
        if not content:
            continue
        tokens = len(_tokenize(content))
        out.append({"role": role, "content": content, "tokens": tokens})
    return out


def _chunk_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def evaluate_qa(
    prompt: str,
    predicted: str,
    gold: str,
    *,
    messages: list[dict] | None = None,
    judge_llm=None,
) -> dict:
    """M5-2 extended:返回 dict 含 chunk_usefulness。"""
    f1 = token_f1(predicted, gold)
    semantic = await semantic_f1(prompt, predicted, gold, judge_llm)
    quality = quality_score(prompt, predicted, gold)
    pass_ = (semantic > 0.7) if semantic is not None else (f1 > 0.5)

    chunk_usefulness: list[dict] = []
    if messages is not None and judge_llm is not None:
        for c in _chunk_messages(messages):
            try:
                score = await judge_chunk_usefulness(
                    c["content"], prompt, gold, judge_llm,
                )
            except Exception:
                score = 0.0
            chunk_usefulness.append({
                "role": c["role"],
                "tokens": c["tokens"],
                "useful_score": score,
            })

    return {
        "f1": f1,
        "semantic_f1": semantic,
        "quality": quality,
        "pass": pass_,
        "chunk_usefulness": chunk_usefulness,
        "trace_payload": {
            "f1": f1,
            "semantic_f1": semantic,
            "quality": quality,
            "pass": pass_,
            "chunk_usefulness_n": len(chunk_usefulness),
        },
    }
```

顶部加 `import hashlib`。

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator_v3.py -v
```

Expected: PASS,8 tests(= 4 chunk + 4 evaluate_qa)。

### Step 5: 跑全 test_evaluator.py,确认 M5-1 路径未坏

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator.py eval/locomo/tests/test_evaluator_v3.py -v
```

Expected: ALL PASS。

### Step 6: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/evaluator.py eval/locomo/tests/test_evaluator_v3.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): evaluator.evaluate_qa 扩展 + chunk_usefulness 端到端

加 messages=None kwarg,evaluate_qa 内部切 chunk (assistant 跳过),
逐 chunk judge → 0/0.5/1。messages 不传 → chunk_usefulness=[]。
token_f1 / semantic_f1 / quality / pass 行为不变(回归测试守护)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: `report._summary_cards_v3` + 5 个 sub-table 函数

**Files:**
- Modify: `eval/locomo/report.py`
- Modify: `eval/locomo/tests/test_report.py`(追加)

**Interfaces:**
- Consumes: `metrics: dict`(5-key)+ `results: list[dict]`(raw 给折叠)
- Produces: HTML 段(5 卡 + 5 sub-table + raw `<details>`)

> 这是这 spec 最大的单文件改动。拆为若干函数,每个测试单独覆盖。
> 旧 `_summary_cards` / `_q_type_table` **保留**(`metrics_v3: false` 路径用)。

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_report.py`:

```python
def test_summary_cards_v3_renders_5_cards():
    from eval.locomo import report
    metrics = {
        "1_recall":      {"n_eligible": 384, "precision": 0.74, "recall": 0.62},
        "2_timeliness":  {"n": 96, "pass_rate": 0.78, "f1_med": 0.62, "semantic_f1_med": 0.71},
        "3_utilization": {"avg": 0.31, "p50": 0.27, "p90": 0.58, "n": 1986, "min": 0.05, "max": 0.83},
        "4_compaction":  {"by_tier": [
            {"tier": 0, "trigger_n": 1450, "avg_retain": None, "pass_rate": 0.71},
            {"tier": 1, "trigger_n": 380, "avg_retain": 0.84, "pass_rate": 0.69},
            {"tier": 2, "trigger_n": 110, "avg_retain": 0.61, "pass_rate": 0.58},
            {"tier": 3, "trigger_n": 46, "avg_retain": 0.42, "pass_rate": 0.31},
        ], "total_compressed_n": 536, "overall_avg_retain": 0.62},
        "5_consistency": {"n_groups": 47, "drift_rate": 0.13, "by_sample": []},
    }
    html = report._summary_cards_v3(metrics)
    assert "记忆召回" in html
    assert "时效性" in html
    assert "利用率" in html
    assert "压缩率" in html
    assert "一致性" in html
    # 数值
    assert "0.74" in html
    assert "0.78" in html
    assert "0.31" in html


def test_subtable_uncomputed_renders_dash():
    from eval.locomo import report
    metrics = {
        "1_recall": "uncomputed",
        "2_timeliness": {"n": 0, "pass_rate": None, "f1_med": None, "semantic_f1_med": None},
        "3_utilization": "uncomputed",
        "4_compaction": {"by_tier": [], "total_compressed_n": 0, "overall_avg_retain": None},
        "5_consistency": "uncomputed",
    }
    html = report._summary_cards_v3(metrics)
    # 主体内容仍渲染,uncomputed 处显示 -
    assert "-" in html


def test_compaction_subtable_v2_includes_all_tiers():
    from eval.locomo import report
    metrics = {"4_compaction": {"by_tier": [
        {"tier": 0, "trigger_n": 1, "avg_retain": None, "pass_rate": 0.5},
        {"tier": 1, "trigger_n": 2, "avg_retain": 0.7, "pass_rate": 0.5},
        {"tier": 2, "trigger_n": 0, "avg_retain": None, "pass_rate": None},
        {"tier": 3, "trigger_n": 0, "avg_retain": None, "pass_rate": None},
    ], "total_compressed_n": 2, "overall_avg_retain": 0.7}}
    html = report._compaction_subtable_v2(metrics["4_compaction"])
    for tier_n in (0, 1, 2, 3):
        assert f">tier {tier_n}<" in html or f">{tier_n}<" in html
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py::test_summary_cards_v3_renders_5_cards -v
```

Expected: FAIL `AttributeError: ... has no attribute '_summary_cards_v3'`。

### Step 3: 实现 `_summary_cards_v3` + 5 sub-table

在 `report.py` 末尾追加(已有 `_summary_cards` 保留):

```python
def _card_val(metrics_key: str | dict, field: str) -> str:
    """'uncomputed' / None → '-',否则 fmt 为 3 位小数。"""
    if isinstance(metrics_key, str):
        return "-"
    if metrics_key is None:
        return "-"
    v = metrics_key.get(field)
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _summary_cards_v3(metrics: dict) -> str:
    """M5-2:5 张顶层 metric 卡。"""
    cards = [
        ("#1 记忆召回",
         [("n_eligible", _card_val(metrics.get("1_recall"), "n_eligible")),
          ("precision",  _card_val(metrics.get("1_recall"), "precision")),
          ("recall",     _card_val(metrics.get("1_recall"), "recall"))]),
        ("#2 时效性",
         [("n",         _card_val(metrics.get("2_timeliness"), "n")),
          ("pass_rate", _card_val(metrics.get("2_timeliness"), "pass_rate")),
          ("f1_med",    _card_val(metrics.get("2_timeliness"), "f1_med"))]),
        ("#3 利用率",
         [("avg", _card_val(metrics.get("3_utilization"), "avg")),
          ("p50", _card_val(metrics.get("3_utilization"), "p50")),
          ("p90", _card_val(metrics.get("3_utilization"), "p90"))]),
        ("#4 压缩率",
         [("tier 1-3 trigger_n",
           _card_val(metrics.get("4_compaction"), "total_compressed_n")),
          ("overall_retain",
           _card_val(metrics.get("4_compaction"), "overall_avg_retain")),
          ("(详见 sub-table)", "")]),
        ("#5 一致性",
         [("n_groups",  _card_val(metrics.get("5_consistency"), "n_groups")),
          ("drift_rate",_card_val(metrics.get("5_consistency"), "drift_rate")),
          ("(详见 sub-table)", "")]),
    ]
    out = ['<div class="metrics-v3-cards">']
    for title, rows in cards:
        out.append('<div class="metric-card">')
        out.append(f'<h3>{title}</h3>')
        for label, val in rows:
            out.append(f'<div class="metric-row"><span>{label}</span><b>{val}</b></div>')
        out.append('</div>')
    out.append('</div>')
    return "\n".join(out)


def _recall_subtable(metrics_1_recall) -> str:
    if isinstance(metrics_1_recall, str):
        return '<p>1. 记忆召回: judge 未配置 —</p>'
    return f"""
<h4>1. 记忆召回(n_eligible={metrics_1_recall.get("n_eligible","-")})</h4>
<p>precision: {_card_val(metrics_1_recall, "precision")},
   recall: {_card_val(metrics_1_recall, "recall")},
   total_recall: {_card_val(metrics_1_recall, "n_total_recall")}</p>
"""


def _timeliness_subtable(metrics_2_timeliness) -> str:
    if not isinstance(metrics_2_timeliness, dict):
        return '<p>2. 时效性: 数据不可得 —</p>'
    return f"""
<h4>2. 时效性(Temporal 子集)</h4>
<p>n={_card_val(metrics_2_timeliness, "n")} ·
   pass_rate={_card_val(metrics_2_timeliness, "pass_rate")} ·
   f1_med={_card_val(metrics_2_timeliness, "f1_med")} ·
   semantic_f1_med={_card_val(metrics_2_timeliness, "semantic_f1_med")}</p>
"""


def _utilization_subtable(metrics_3_utilization) -> str:
    if isinstance(metrics_3_utilization, str):
        return '<p>3. 利用率: chunk_usefulness 空 —</p>'
    return f"""
<h4>3. 利用率</h4>
<p>avg={_card_val(metrics_3_utilization, "avg")} ·
   p50={_card_val(metrics_3_utilization, "p50")} ·
   p90={_card_val(metrics_3_utilization, "p90")} ·
   n={_card_val(metrics_3_utilization, "n")} ·
   min={_card_val(metrics_3_utilization, "min")} ·
   max={_card_val(metrics_3_utilization, "max")}</p>
"""


def _compaction_subtable_v2(metrics_4_compaction) -> str:
    if not isinstance(metrics_4_compaction, dict):
        return '<p>4. 压缩率: 数据不可得 —</p>'
    rows = []
    for row in metrics_4_compaction.get("by_tier", []):
        rows.append(
            f"<tr><td>{row['tier']}</td>"
            f"<td>{_card_val(row, 'trigger_n')}</td>"
            f"<td>{_card_val(row, 'avg_retain')}</td>"
            f"<td>{_card_val(row, 'pass_rate')}</td></tr>"
        )
    return f"""
<h4>4. 压缩率(per-tier)</h4>
<table class="subtable"><thead><tr><th>tier</th><th>trigger_n</th><th>avg_retain</th><th>pass_rate</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p>total_compressed_n={metrics_4_compaction.get('total_compressed_n','-')};
   overall_avg_retain={_card_val(metrics_4_compaction, "overall_avg_retain")}</p>
"""


def _consistency_subtable(metrics_5_consistency) -> str:
    if isinstance(metrics_5_consistency, str):
        return '<p>5. 一致性: judge 未配置 —</p>'
    by_sample = metrics_5_consistency.get("by_sample") or []
    rows = []
    for s in by_sample:
        rows.append(
            f"<tr><td>{s.get('sample_id','-')}</td>"
            f"<td>{_card_val(s, 'n_groups')}</td>"
            f"<td>{_card_val(s, 'drift_groups')}</td>"
            f"<td>{_card_val(s, 'drift_rate')}</td></tr>"
        )
    return f"""
<h4>5. 一致性(per-sample)</h4>
<p>n_groups={_card_val(metrics_5_consistency, "n_groups")};
   drift_rate={_card_val(metrics_5_consistency, "drift_rate")}</p>
<table class="subtable"><thead><tr><th>sample_id</th><th>n_groups</th><th>drift_groups</th><th>drift_rate</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
"""
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py -v
```

Expected: PASS,3 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/report.py eval/locomo/tests/test_report.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): report._summary_cards_v3 + 5 sub-table 函数

5 卡 + 5 sub-table(uncomputed → '-')。旧 _summary_cards / _q_type_table
保留作 metrics_v3:false 路径。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 11: `report.write_html_report` 集成 + raw 折叠

**Files:**
- Modify: `eval/locomo/report.py`
- Modify: `eval/locomo/tests/test_report.py`(追加)

**Interfaces:**
- Consumes: `out_path, results, metrics, metrics_v3: bool = True`
- Produces: 写一个 HTML 文件

> 默认进 v3 路径;`metrics_v3=False` 走旧卡(回归兼容)。

### Step 1: 写失败测试

追加到 `eval/locomo/tests/test_report.py`:

```python
def test_write_html_report_v3_uses_5_cards(tmp_path):
    from eval.locomo import report
    out = tmp_path / "report.html"
    results = [
        {"sample_id": "x", "q_type": "3", "pass": True, "f1": 0.5, "semantic_f1": 0.6,
         "predicted": "p", "gold": "g", "prompt_tokens": 100,
         "tool_calls": [], "compaction": None, "chunk_usefulness": []},
    ]
    metrics = {
        "1_recall": "uncomputed",
        "2_timeliness": {"n": 1, "pass_rate": 1.0, "f1_med": 0.5, "semantic_f1_med": 0.6},
        "3_utilization": "uncomputed",
        "4_compaction": {"by_tier": [], "total_compressed_n": 0, "overall_avg_retain": None},
        "5_consistency": "uncomputed",
    }
    report.write_html_report(str(out), results, metrics, metrics_v3=True)
    text = out.read_text(encoding="utf-8")
    # 5 卡 标记
    assert "#1 记忆召回" in text
    assert "时效性" in text
    # raw records 在 <details> 折叠
    assert "<details>" in text


def test_write_html_report_v3_false_uses_legacy(tmp_path):
    from eval.locomo import report
    out = tmp_path / "report.html"
    results = [
        {"sample_id": "x", "q_type": "1", "pass": True, "f1": 0.5,
         "predicted": "p", "gold": "g", "prompt_tokens": 100},
    ]
    # metrics_v3=False → 旧 _summary_cards 路径
    metrics_legacy = {"by_q_type": {}, "compaction": {}, "utilization": {}, "memory": "uncomputed", "tool_accuracy": "uncomputed"}
    report.write_html_report(str(out), results, metrics_legacy, metrics_v3=False)
    text = out.read_text(encoding="utf-8")
    # v3 卡 不应出现
    assert "metrics-v3-cards" not in text
    # 旧 summary_cards 路径有 q_type / 等关键词
    # (旧 _summary_cards 包含 sample / pass 等;允许 flexibly 不必检验,只要 v3 标记不在)
```

### Step 2: 跑测试确认失败

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py::test_write_html_report_v3_uses_5_cards -v
```

Expected: FAIL(签名 `write_html_report` 不收 `metrics_v3` kwarg)。

### Step 3: 改 `write_html_report`

> 先打开 `report.py` 看现有 `write_html_report` 的实现(M5-1 spec §3.6 描述过)。

例如:

```python
def write_html_report(
    out_path: str,
    results: list[dict],
    metrics: dict | None = None,
    *,
    metrics_v3: bool = True,
) -> None:
    """M5-2:metrics_v3=True 走 5 卡新路径;False 走 M5-1 旧 cards。"""
    css = """ ... existing inline css ..."""
    head = f"<html><head><meta charset='utf-8'><title>Locomo Report</title><style>{css}</style></head><body>"
    if metrics_v3:
        cards_block = _summary_cards_v3(metrics or {})
        subtables = (
            _recall_subtable((metrics or {}).get("1_recall"))
            + _timeliness_subtable((metrics or {}).get("2_timeliness"))
            + _utilization_subtable((metrics or {}).get("3_utilization"))
            + _compaction_subtable_v2((metrics or {}).get("4_compaction"))
            + _consistency_subtable((metrics or {}).get("5_consistency"))
        )
    else:
        cards_block = _summary_cards(results, metrics)
        subtables = _q_type_table((metrics or {}).get("by_q_type") or {}) if metrics else ""
    raw = (
        '<details><summary>raw per-record data(展开)</summary>'
        + _raw_records_table(results)
        + '</details>'
    )
    body = f"<h1>Locomo Eval Report — {'M5-2 metrics v3' if metrics_v3 else 'M5-1 legacy'}</h1>"
    body += cards_block + subtables + raw + "</body></html>"
    Path(out_path).write_text(body, encoding="utf-8")


def _raw_records_table(results: list[dict]) -> str:
    """每条 QA 一行:sample / q / pred / gold / pass / f1 / sem / quality / cost / tools。"""
    headers = ["sample_id", "q_type", "pass", "f1", "semantic_f1", "quality", "tokens", "cost"]
    rows_html = []
    for r in results:
        cells = [
            r.get("sample_id", ""),
            r.get("q_type", ""),
            "✓" if r.get("pass") else "✗",
            f"{r.get('f1', 0):.3f}" if r.get('f1') is not None else "-",
            f"{r.get('semantic_f1', 0):.3f}" if r.get('semantic_f1') is not None else "-",
            f"{r.get('quality', 0):.3f}" if r.get('quality') is not None else "-",
            str(r.get("prompt_tokens", "")),
            f"{r.get('cost_usd', 0):.4f}" if r.get('cost_usd') is not None else "-",
        ]
        rows_html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return (
        "<table class='raw-table'><thead><tr>"
        + "".join(f"<th>{h}</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )
```

### Step 4: 跑测试确认通过

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py -v
```

Expected: PASS,5 tests。

### Step 5: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/report.py eval/locomo/tests/test_report.py
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): write_html_report 集成 + raw 折叠 + 双轨

metrics_v3=True(默认)走 5 卡 + 5 sub-table;False 走 M5-1 旧 cards。
raw per-record 默认 <details> 折叠。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 12: `runner.py` 多处改动 + `policy_local.yaml` 双轨开关(SCOPE 扩展 — 涵盖原 1-line + Task 7 run_judge 迁移 + dataset_sha 计算)

> **本 Task 在原 plan scope 上扩展(2026-07-20 实施期发现)**:`run_judge` Task 7 改写后(runner.py:484 调用未更新),runner 进入新指标体系需要进一步改动。本任务一并收口。

**Files:**
- Modify: `eval/locomo/runner.py`(多处改动,见 Steps)
- Modify: `eval/locomo/policy_local.yaml`(2 keys)

**Interfaces Touched (in runner.py):**
1. `evaluate_qa` call site → 加 `messages=qa_messages` kwarg(M5-2 指标 3 chunk_usefulness 上游需要)
2. `run_judge` call site → 从旧同步 4-arg `(all_results, qas, judge_llm, judge_cache)` 改为新异步 6-arg `await run_judge(all_results, qas, conversations_dict, judge_llm, cache_path, dataset_sha)`(Task 7 重写产物的对应迁移)
3. 新增 `dataset_sha` 计算 — `hashlib.sha256(open(data_file,'rb').read()).hexdigest()[:8]`,cache 路径用这个 key
4. 新增 `conversations_dict` 构造 — 从当前 sample 列表的 conversation,按 `sample_id` 索引成 dict(compute_recall 契约)

**Interface contract (policy_local.yaml 新增):**

```yaml
locomo_eval:
  enabled: true
  trace_to_langfuse: true
  max_turns_per_sample: 500
  sample_timeout_s: 1800
  inject_memory_tools: true
  clear_memory_tags: ["locomo/"]
  metrics_v3: true             # NEW: M5-2 新指标体系
  judge_chunk_usefulness: true # NEW: evaluator 跑 chunk judge
```

**执行顺序**(每段独立可验):

### Step 1: 修改 `evaluate_qa` 调用 — 加 `messages=`

打开 `runner.py`,找 `eval_result = await evaluate_qa(qa.question, predicted, qa.answer, judge_llm=llm)`,改为:

```python
eval_result = await evaluate_qa(
    qa.question, predicted, qa.answer,
    messages=qa_messages, judge_llm=llm,
)
```

### Step 2: 修改 `run_judge` 调用 — 旧 4-arg → 新 6-arg async

找到 runner.py:484(原 4-arg 调用,可能在 Task 11 后偏移几行),改为:

```python
import asyncio
import hashlib

# 在 sample 处理循环后,add:
conversations_by_sample_id = {}
for parsed_conv, _ in conv_iter_pairs:  # 用 sample_list 的 conversation 列表
    sid = parsed_conv.sample_id  # 视具体 runner 结构调整
    conversations_by_sample_id[sid] = parsed_conv.conversation

dataset_sha = hashlib.sha256(open(data_file, "rb").read()).hexdigest()[:8]
metrics = await run_judge(
    all_results,
    all_qas,                     # 平行 list,与 all_results 对齐
    conversations_by_sample_id, # Dict[sample_id, conv]
    judge_llm=llm,
    cache_path=Path(".report-cache"),
    dataset_sha=dataset_sha,
)
```

> **Note**:具体 runner 内的数据结构(怎么拿 `parsed_conv` / `sample_id` / `data_file` 路径)需要 implementer 在 `runner.py` 内搜索 `sample_id` / `parsed_conv` / `DEFAULT_FILE` 来定位。adjust as needed

### Step 3: 改 `policy_local.yaml`

若文件已存在,只追加 2 keys;不存在则新建完整 default block(同 §4.5 spec)。

### Step 4: 端到端 smoke 验证

```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/ -v
```

Expected: ALL PASS(incl. M5-2 新增 + M5-1 遗留)。1 已知预存失败 `test_runner_smoke_*`(Task 5 implementer 标记) — 与本 Task 无关。

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --no-memory-tools
```

Expected: 看到 `[smoke OK]` + `eval/result/locomo-*.html` 落地;HTML 应含 5 张 metric_v3 标记。

### Step 5: Commit

分别对 runner.py / policy_local.yaml / runner.py 后续更新可合并为单 commit(若改动集中)。理想 commit:

```
feat(locomo-m5-2): runner integrate evaluate_qa messages + run_judge 6-arg + policy 双轨

evaluate_qa 调用多传 messages=qa_messages;run_judge 调用从旧同步 4-arg
改为新异步 6-arg(配套 asyncio.run 或 await 上下文);构造 conversations dict
按 sample_id 索引;dataset_sha from sha256(locomo10.json)[:8]。

policy_local.yaml 加 metrics_v3: true + judge_chunk_usefulness: true
作为双轨开关(false → runner 旧报告路径)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

**Files:**
- Modify: `eval/locomo/runner.py`(1 行)
- Modify: `eval/locomo/policy_local.yaml`(2 keys)
- Modify: `eval/locomo/tests/test_runner_smoke.py`(仅当 default 改了需更新)

**Interfaces:**
- runner 改:多传 `messages=qa_messages` 给 evaluate_qa
- policy 加:`metrics_v3: true` + `judge_chunk_usefulness: true`

### Step 1: 改 runner

打开 `runner.py`,找 `eval_result = await evaluate_qa(qa.question, predicted, qa.answer, judge_llm=llm)`(line 281),改为:

```python
eval_result = await evaluate_qa(
    qa.question, predicted, qa.answer,
    messages=qa_messages, judge_llm=llm,
)
```

只此 1 行。其它逻辑不动。

### Step 2: 改 policy_local.yaml

在 `eval/locomo/policy_local.yaml`(若已有)末尾追加;若不存在则创建:

```yaml
locomo_eval:
  enabled: true
  trace_to_langfuse: true
  max_turns_per_sample: 500
  sample_timeout_s: 1800
  inject_memory_tools: true
  clear_memory_tags: ["locomo/"]
  metrics_v3: true             # NEW: M5-2 新指标体系
  judge_chunk_usefulness: true # NEW: evaluator 跑 chunk judge
```

### Step 3: 跑 smoke 验证

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/ -v
```

Expected: ALL PASS(全部既存测试仍通过 + M5-2 新测试)。

### Step 4: 端到端 smoke(--limit 1,不开 memory tools,无 trace)

Run(先确认 download 完成):
```bash
cd D:/agent_learning/cc-harness && ls eval/locomo/data/locomo10.json 2>/dev/null || PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/download_dataset.py
```

Run smoke:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --no-memory-tools 2>&1 | tail -40
```

Expected: 看到 `[smoke OK]`,exit 0,HTML 文件落地到 `eval/result/locomo-*.html`。

### Step 5: 验证 HTML 含 5 卡

```bash
cd D:/agent_learning/cc-harness && ls eval/result/locomo-*.html | head -1 | xargs grep -E "记忆召回|时效性|利用率|压缩率|一致性" | head -5
```

Expected: 5 个标签都命中。

### Step 6: Commit

```bash
cd D:/agent_learning/cc-harness && git add eval/locomo/runner.py eval/locomo/policy_local.yaml
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit -m "feat(locomo-m5-2): runner 1 行 + policy_local 双轨开关

evaluate_qa 多传 messages=;policy_local.yaml 加 metrics_v3 / judge_chunk_usefulness。
旧 metrics_v3:false 路径自动走 M5-1 旧 _summary_cards。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 13: 全量 10 样本回归 + 双轨 diff

**Files:**
- 不改代码;只验证 + 写 final commit。

**Goal**:通过 §1.4 AC2 / AC6:全量 10 样本跑通;`metrics_v3: true` 与 `false` 两条路径都产 HTML。

### Step 1: 跑 `metrics_v3: true` 全量

先验证 dataset、env 等都齐(judge LLM API key 在 `.env`)。

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py 2>&1 | tail -40
```

Expected:看到 `[run finished]` + `eval/result/locomo-report-YYYY-MM-DD.html` 落地。

### Step 2: 验证 HTML 5 卡 + sub-table 全部非空 / 有数据

```bash
cd D:/agent_learning/cc-harness && ls -la eval/result/locomo-report-*.html
LATEST=$(ls eval/result/locomo-report-*.html | tail -1)
echo "5-cards check:"; grep -E "记忆召回|时效性|利用率|压缩率|一致性" "$LATEST" | wc -l
echo "sub-table check:"; grep -E "compaction_subtable|timeliness_subtable|consistency_subtable|utilization_subtable|recall_subtable" "$LATEST" | wc -l
echo "raw-folded check:"; grep -c "<details>" "$LATEST"
```

Expected:
- 5-cards: ≥5
- sub-table: ≥5  
- raw-folded: ≥1

### Step 3: 切到 `metrics_v3: false` 跑一次,验证旧路径仍可用

Edit `eval/locomo/policy_local.yaml`: `metrics_v3: false`。再跑:

```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --resume 2>&1 | tail -20
```

Expected:HTML 文件落地,v3 标记 `metrics-v3-cards` 不在 HTML 中(说明走旧路径)。

切回 `metrics_v3: true`。

### Step 4: 验证缓存命中(AC6)

第二次跑全量:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --resume 2>&1 | tail -20
```

观察输出:应无新 LLM judge 调用(从 stdout/日志确认;judge LLM 请求次数应明显小于第一次)。

### Step 5: 跑全 pytest

Run:
```bash
cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/ -v
```

Expected: ALL PASS(13+ 件 test_metrics_v3 + test_evaluator_v3 + test_metrics + test_evaluator + test_report + test_runner_smoke + test_dataset + test_dataset_session_index)。

### Step 6: Final commit(policy + result 索引)

```bash
cd D:/agent_learning/cc-harness && git add eval/result/  # 仅索引文件(可选,locomo-result/ 通常 gitignore)
git -c user.email=claude-fable-5@noreply.anthropic.com -c user.name="Claude Fable 5" \
  commit --allow-empty -m "chore(locomo-m5-2): v3 full-sample end-to-end validation complete

AC1-AC6 全验证通过:5 卡 + sub-table + raw 折叠,judge 缓存命中,
metrics_v3:false 旧路径仍可用。13+ unit tests 全过。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

如果 `eval/result/` 在 `.gitignore`,改成 `--allow-empty`(空 commit)或 commit 一个 status 摘要文件 `eval/result/locomo-m5-2-final-status.md`(手工写)。

---

## Self-Review Checklist(Run Before Handoff)

- [ ] Spec 8 项 AC(§1.4)对应到 Task 1-13(yes)
  - AC1 → Task 12 step 4-5
  - AC2 → Task 13
  - AC3 → Task 7 + report.py
  - AC4 → 全部 task 跑 `-m pytest -v`
  - AC5 → Task 9
  - AC6 → Task 13 step 4
- [ ] 5 个 metric 全覆盖(Task 1-6 + 整合到 Task 7)
- [ ] 旧 API 不破坏(Task 7 step 5 验证)
- [ ] 双轨 `metrics_v3` 生效(Task 11 + Task 12)
- [ ] 缓存键含 `dataset_sha`(Task 7 + Task 13)
- [ ] 测试有 failing-test-first 覆盖每个 task
- [ ] 全 commit 通过 CLAUDE.md `Co-Authored-By` 格式
- [ ] 无 TBD / TODO / FIXME
- [ ] Windows 入口 PYTHONIOENCODING=utf-8 一致

## Out of Scope(reminders)

- 不要改 `cc_harness/`(agent / policy / memory / tokens / llm)
- 不要加新依赖
- 不要改 LoCoMo10.json 数据集
- 不要做时间序列折线图(避免 plot.js 重依赖)
- 不在 metrics_v3:false 路径下运行本计划新 metric 函数

---

## 执行入口

**Two execution options:**

**1. Subagent-Driven (recommended)** —— 每个 Task 派一个新 subagent 执行,Task 间 review,迭代快。

**2. Inline Execution** —— 在当前 session 顺序执行,每 Task 完成 checkpoint。

任选其一进入执行阶段。
