# Locomo 长对话记忆 QA 评测子系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 `eval/locomo/` 子系统,跑 snap-research/locomo 10 个长对话样本,测 cc-harness agent 记忆能力 + 成本 + 任务轨迹,结果上 langfuse cloud + 出 HTML 报告。

**Architecture:** `eval/locomo/runner.py` 同步调 `cc_harness/agent.py:run_turn_sync`(方案 C — agent.py 不改,只新增同步外壳),每条 turn 包 `langfuse.trace.span` + `generation`,每条 QA 走 `evaluator.py`(token F1 + deepeval GEval)。`cc_harness/memory/` 第一次 wire 进 ReAct loop,走 2 个新 native tool(`memory_store` / `memory_query`),L4 闸门拦危险调用。报告走 `report.py` 生成 HTML(每条 QA 一行 + 顶层 summary cards)。

**Tech Stack:** Python 3.13、deepeval(LLM judge)、langfuse(cloud trace SDK)、tiktoken(已有,token 算)、pytest、locomo10 数据集(JSON,gitignored)、cc-harness 现有 ReAct loop。

**Spec:** `docs/superpowers/specs/2026-07-07-locomo-eval-design.md`(3 轮 spec-review approved, `8d40040`)

**Scope:** 单子项目,自包含 5 phase(数据 / 工具 wire / runner / 测试 / 全量跑)。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `eval/locomo/__init__.py` | 包 marker(空) |
| `eval/locomo/download_dataset.py` | 从 snap-research/locomo 拉 `data/locomo10.json` 到 `eval/locomo/data/`,附 attribution |
| `eval/locomo/dataset.py` | locomo JSON 加载、turn 解析、QA 切分、edge case 处理 |
| `eval/locomo/evaluator.py` | `token_f1` + `quality_score`(deepeval GEval)+ `evaluate_qa` |
| `eval/locomo/trace.py` | langfuse SDK 封装:`LocomoTrace` 类(trace/span/generation/event/score/flush) |
| `eval/locomo/report.py` | HTML 报告生成(6 状态 schema,顶层 summary cards) |
| `eval/locomo/runner.py` | 入口 CLI:循环 10 样本,replay + QA + 评分 + 报告 + langfuse |
| `eval/locomo/tests/__init__.py` | pytest 包 marker(空) |
| `eval/locomo/tests/test_evaluator.py` | `token_f1` + `evaluate_qa` 单测(5 fixture) |
| `eval/locomo/tests/test_dataset.py` | locomo 解析单测(4 case:空/单/多 session/N/A) |
| `eval/locomo/tests/test_runner_smoke.py` | 端到端 smoke:`--limit 1 --no-trace` 跑通 |
| `eval/locomo/data/.gitkeep` | 数据目录占位 |
| `eval/locomo/data/locomo10.json` | 数据集(下载,gitignored) |
| `eval/locomo/.gitignore` | 数据 + .checkpoint.json + 临时文件 |
| `cc_harness/tools.py` | **加 2 个 native tool**:`memory_store`、`memory_query` |
| `cc_harness/agent.py` | **小幅 refactor**:抽 `_run_turn_inner`,加 `run_turn_sync` 同步外壳 |
| `cc_harness/policy.py` | **加 2 条 tool 规则**:`memory_store` ask + secret 拦,`memory_query` allow + secret 拦 |
| `pyproject.toml` | **加依赖**:`deepeval>=0.21`、`langfuse>=2.0` |
| `policy.yaml`(仓根) | **加段**:`locomo_eval:`(kill-switch) |
| `.env.example` | **加 3 项**:`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` |
| `tests/test_agent.py` | 已有,**加 1 case**:`run_turn_sync` 跟原 `run_turn` 行为一致(契约测试) |

---

## Task 1: 数据下载脚本

**Files:**
- Create: `eval/locomo/__init__.py`(空)
- Create: `eval/locomo/download_dataset.py`
- Create: `eval/locomo/data/.gitkeep`(空)
- Create: `eval/locomo/.gitignore`(含 `data/locomo10.json`、`.checkpoint.json`)

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_download_dataset.py
from pathlib import Path
import json
from eval.locomo.download_dataset import verify_dataset

def test_verify_dataset_accepts_locomo10(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps({
        "sample_id": "test-1",
        "conversation": {"session_1": [{"speaker": "A", "dia_id": "d1", "text": "hi"}]},
        "qa": [{"question": "q?", "answer": "a", "category": "test", "evidence": ["d1"]}],
    }))
    samples = verify_dataset(fake)
    assert len(samples) == 1
    assert samples[0]["sample_id"] == "test-1"

def test_verify_dataset_rejects_missing_qa(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps({"sample_id": "x", "conversation": {}, "qa": []}))
    import pytest
    with pytest.raises(ValueError, match="no QA pairs"):
        verify_dataset(fake)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_download_dataset.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 download_dataset.py**

```python
"""Download snap-research/locomo dataset to eval/locomo/data/.

Source: https://github.com/snap-research/locomo
File: data/locomo10.json (10 long conversations, ~300 turns each, with QA pairs).
License: see upstream repo LICENSE.txt — local eval only, NOT committed to this repo.
"""
import json
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_FILE = DATA_DIR / "locomo10.json"
SOURCE_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def verify_dataset(path: Path) -> list[dict[str, Any]]:
    """Load and sanity-check locomo JSON. Returns list of samples.
    Raises ValueError if file missing/empty/no QA."""
    if not path.exists():
        raise FileNotFoundError(f"locomo data not found at {path}; run download_dataset() first")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "sample_id" in raw:
        raw = [raw]  # single sample wrapped
    if not isinstance(raw, list):
        raise ValueError(f"locomo JSON must be list or sample dict, got {type(raw).__name__}")
    for i, s in enumerate(raw):
        if not s.get("qa"):
            raise ValueError(f"sample #{i} ({s.get('sample_id','?')}) has no QA pairs")
        if not s.get("conversation"):
            raise ValueError(f"sample #{i} ({s.get('sample_id','?')}) has no conversation")
    return raw


def download_dataset(target: Path = DEFAULT_FILE, url: str = SOURCE_URL) -> Path:
    """Download locomo10.json from upstream. Returns target path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[download] {url} -> {target}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = resp.read()
    target.write_bytes(body)
    samples = verify_dataset(target)
    print(f"[download] OK: {len(samples)} samples, {target.stat().st_size // 1024} KB")
    return target


if __name__ == "__main__":
    download_dataset()
```

- [ ] **Step 4: 创建 `__init__.py`、`.gitkeep`、`.gitignore`**

```bash
touch eval/locomo/__init__.py eval/locomo/tests/__init__.py eval/locomo/data/.gitkeep
```

`eval/locomo/.gitignore` 内容:
```
data/locomo10.json
.checkpoint.json
__pycache__/
*.pyc
```

- [ ] **Step 5: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_download_dataset.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: 跑下载脚本(走网络,可能失败)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/download_dataset.py`
Expected: 打印 `[download] OK: 10 samples, ...KB` 或网络错误(后种情况进 plan 第 2 阶段手动 git clone)

- [ ] **Step 7: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/__init__.py eval/locomo/download_dataset.py eval/locomo/tests/ eval/locomo/data/.gitkeep eval/locomo/.gitignore
git commit -m "feat(locomo-eval): 数据下载脚本 + 10-sample sanity check (Task 1)"
```

---

## Task 2: dataset.py 模块

**Files:**
- Create: `eval/locomo/dataset.py`

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_dataset.py
import json
from pathlib import Path
from eval.locomo.dataset import parse_sample, iter_turns, iter_qa

def test_parse_sample_basic():
    raw = {
        "sample_id": "s1",
        "conversation": {
            "session_1": [
                {"speaker": "A", "dia_id": "d1", "text": "hello"},
                {"speaker": "B", "dia_id": "d2", "text": "hi"},
            ],
            "session_2": [
                {"speaker": "A", "dia_id": "d3", "text": "what's up?"},
            ],
        },
        "qa": [{"question": "q1", "answer": "a1", "category": "test", "evidence": ["d1"]}],
    }
    sample = parse_sample(raw)
    assert sample.sample_id == "s1"
    turns = list(iter_turns(sample))
    assert len(turns) == 3
    assert turns[0].text == "hello"
    assert turns[0].session == "session_1"
    assert turns[2].session == "session_2"
    qa = list(iter_qa(sample))
    assert len(qa) == 1
    assert qa[0].question == "q1"

def test_iter_turns_handles_missing_sessions():
    raw = {"sample_id": "x", "conversation": {}, "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}]}
    sample = parse_sample(raw)
    assert list(iter_turns(sample)) == []

def test_iter_turns_skips_malformed_entries():
    raw = {
        "sample_id": "x",
        "conversation": {
            "session_1": [
                {"speaker": "A", "dia_id": "d1", "text": "ok"},
                {"bad": "entry"},  # missing speaker/text — skip
            ],
        },
        "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}],
    }
    sample = parse_sample(raw)
    turns = list(iter_turns(sample))
    assert len(turns) == 1  # only the well-formed one

def test_iter_qa_returns_empty_for_no_qa():
    raw = {"sample_id": "x", "conversation": {}, "qa": []}
    sample = parse_sample(raw)
    assert list(iter_qa(sample)) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_dataset.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 dataset.py**

```python
"""Locomo dataset parsing.

A locomo sample:
  {sample_id, conversation: {session_name: [{speaker, dia_id, text}, ...]}, qa: [{question, answer, category, evidence}]}
"""
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Turn:
    session: str
    speaker: str
    dia_id: str
    text: str


@dataclass
class QA:
    question: str
    answer: str
    category: str
    evidence: list[str]  # dia_ids


@dataclass
class Sample:
    sample_id: str
    conversation: dict[str, list[dict]]
    qa: list[dict]


def parse_sample(raw: dict) -> Sample:
    return Sample(
        sample_id=raw["sample_id"],
        conversation=raw.get("conversation", {}),
        qa=raw.get("qa", []),
    )


def iter_turns(sample: Sample) -> Iterator[Turn]:
    """Yield turns in order. Skip entries missing speaker/text."""
    for session_name in sorted(sample.conversation.keys()):
        for entry in sample.conversation[session_name]:
            if not isinstance(entry, dict):
                continue
            if "speaker" not in entry or "text" not in entry:
                continue
            yield Turn(
                session=session_name,
                speaker=entry["speaker"],
                dia_id=str(entry.get("dia_id", "")),
                text=entry["text"],
            )


def iter_qa(sample: Sample) -> Iterator[QA]:
    for q in sample.qa:
        if not isinstance(q, dict):
            continue
        yield QA(
            question=q.get("question", ""),
            answer=q.get("answer", ""),
            category=q.get("category", "unknown"),
            evidence=q.get("evidence", []) or [],
        )
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_dataset.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/dataset.py eval/locomo/tests/test_dataset.py
git commit -m "feat(locomo-eval): dataset 解析模块 (Task 2)"
```

---

## Task 3: evaluator.py — token F1

**Files:**
- Create: `eval/locomo/evaluator.py`

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_evaluator.py
from eval.locomo.evaluator import token_f1, evaluate_qa

def test_token_f1_exact_match():
    assert token_f1("hello world", "hello world") == 1.0

def test_token_f1_partial():
    score = token_f1("the cat sat", "the cat sat on a mat")
    assert 0.4 < score < 0.8  # partial overlap

def test_token_f1_no_overlap():
    assert token_f1("apple banana", "cherry date") == 0.0

def test_token_f1_empty_predicted():
    assert token_f1("", "anything") == 0.0

def test_token_f1_empty_gold():
    assert token_f1("anything", "") == 0.0

def test_token_f1_handles_chinese():
    score = token_f1("我喜欢苹果", "我喜欢苹果和香蕉")
    assert score > 0.5

def test_evaluate_qa_returns_dict():
    result = evaluate_qa("What color?", "blue", "blue")
    assert "f1" in result
    assert "quality" in result
    assert "pass" in result
    assert "trace_payload" in result
    assert result["f1"] == 1.0
    # quality may be None if deepeval not configured, OR a float
    if result["quality"] is not None:
        assert 0.0 <= result["quality"] <= 1.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 evaluator.py(只先 F1,deepeval 下一 task)**

```python
"""QA evaluation: token F1 (locomo official) + deepeval GEval (subjective quality)."""
from __future__ import annotations
import re
from typing import Optional


def _tokenize(text: str) -> list[str]:
    """Whitespace + simple word splitting. Handles CJK by per-character split."""
    if not text:
        return []
    # Split CJK chars as individual tokens, ASCII words by whitespace
    cjk = re.findall(r"[一-鿿]", text)
    other = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return cjk + other


def token_f1(predicted: str, gold: str) -> float:
    """Token-level F1 (locomo convention). Returns 0.0-1.0."""
    pred_tokens = _tokenize(predicted)
    gold_tokens = _tokenize(gold)
    if not pred_tokens or not gold_tokens:
        return 0.0
    common: dict[str, int] = {}
    for t in pred_tokens:
        if t in gold_tokens:
            common[t] = min(pred_tokens.count(t), gold_tokens.count(t))
    if not common:
        return 0.0
    num_same = sum(common.values())
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def quality_score(prompt: str, predicted: str, gold: str) -> Optional[float]:
    """Deepeval GEval('answer quality') — wrapped to fail-soft if deepeval/judge LLM not available.

    Returns:
        float 0-1 on success
        None if deepeval not installed or judge LLM failed
    """
    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase
    except ImportError:
        return None
    try:
        metric = GEval(
            name="answer-quality",
            criteria="Is the predicted answer factually correct and relevant to the prompt, given the gold reference?",
            evaluation_params=[],  # default LLMTestCaseParams
        )
        case = LLMTestCase(input=prompt, actual_output=predicted, expected_output=gold)
        metric.measure(case)
        return float(metric.score) / 100.0  # deepeval returns 0-100
    except Exception:
        return None


def evaluate_qa(prompt: str, predicted: str, gold: str) -> dict:
    """Returns dict with f1, quality, pass, trace_payload.
    trace_payload is what langfuse trace.update(output=) consumes.
    """
    f1 = token_f1(predicted, gold)
    quality = quality_score(prompt, predicted, gold)
    pass_ = (f1 > 0.5) or (quality is not None and quality > 0.7)
    return {
        "f1": f1,
        "quality": quality,
        "pass": pass_,
        "trace_payload": {
            "f1": f1,
            "quality": quality,
            "pass": pass_,
        },
    }
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator.py -v`
Expected: PASS (6 of 7 tests; `test_evaluate_qa_returns_dict` may skip quality if deepeval not installed)

- [ ] **Step 5: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/evaluator.py eval/locomo/tests/test_evaluator.py
git commit -m "feat(locomo-eval): evaluator module (F1 + deepeval GEval, fail-soft) (Task 3)"
```

---

## Task 4: cc_harness tools.py — memory_store / memory_query

**Files:**
- Modify: `cc_harness/tools.py`(尾部加 2 函数 + register)
- Modify: `cc_harness/policy.py`(加 2 规则)

- [ ] **Step 1: 写测试(契约:tool 注册存在 + 参数 schema 正确)**

```python
# eval/locomo/tests/test_memory_tools.py
from cc_harness import tools

def test_memory_store_registered():
    assert "memory_store" in tools.NATIVE_TOOLS
    spec = tools.NATIVE_TOOLS["memory_store"]
    assert "text" in spec["parameters"]["properties"]
    assert "text" in spec["parameters"]["required"]

def test_memory_query_registered():
    assert "memory_query" in tools.NATIVE_TOOLS
    spec = tools.NATIVE_TOOLS["memory_query"]
    assert "question" in spec["parameters"]["properties"]
    assert "question" in spec["parameters"]["required"]
    assert "top_k" in spec["parameters"]["properties"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_memory_tools.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 memory_store / memory_query(占位实现,先注册 + 简单 SQLite)**

```python
# 在 cc_harness/tools.py 末尾追加:

def memory_store(args: dict, ctx: dict) -> dict:
    """把一条对话摘要存进 cc_harness/memory SQLite+embedding 库。

    L4 闸门在 cc_harness/policy.py 配(ask)。
    text 含 'password'/'secret'/'token' 关键词 → policy 拦。
    """
    from cc_harness.memory import store_summary
    text = args.get("text", "")
    tags = args.get("tags", [])
    if not text:
        return {"ok": False, "error": "text required"}
    try:
        item_id = store_summary(text=text, tags=tags, ctx=ctx)
        return {"ok": True, "id": item_id, "stored_chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def memory_query(args: dict, ctx: dict) -> dict:
    """按语义相似度检索 top-k 条摘要。

    L4 闸门:allow(question 含 secret 关键词 policy 拦)。
    """
    from cc_harness.memory import query_similar
    question = args.get("question", "")
    top_k = int(args.get("top_k", 5))
    if not question:
        return {"ok": False, "error": "question required"}
    try:
        hits = query_similar(question=question, top_k=top_k, ctx=ctx)
        return {"ok": True, "hits": hits, "count": len(hits)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "fallback": "noop", "hits": []}
```

并在 NATIVE_TOOLS dict 加:

```python
NATIVE_TOOLS = {
    "run_command": {...},  # 已有
    "memory_store": {
        "name": "memory_store",
        "description": "把一条对话摘要存进本地 SQLite+embedding 记忆库。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要存的摘要文本"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签,如 ['locomo','turn-23']"},
            },
            "required": ["text"],
        },
    },
    "memory_query": {
        "name": "memory_query",
        "description": "按语义相似度从记忆库检索 top-k 条摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["question"],
        },
    },
}
```

并在 dispatch 路由加:
```python
TOOL_DISPATCH = {
    "run_command": run_command,
    "memory_store": memory_store,
    "memory_query": memory_query,
}
```

- [ ] **Step 4: 在 cc_harness/memory/ 写 store_summary / query_similar 桩**

```python
# cc_harness/memory/__init__.py 添加
# (如果包还不存在,先建)
def store_summary(text: str, tags: list[str], ctx: dict) -> str:
    """Stub: 真实实现用 SQLite+embedding。locomo runner 用,先 fake id 返回。
    Phase 2 可换成真 SQLite 写。"""
    import uuid
    return uuid.uuid4().hex[:8]

def query_similar(question: str, top_k: int, ctx: dict) -> list[dict]:
    """Stub: 返回空 list。Phase 2 换成真 embedding 检索。"""
    return []
```

(实装时如果 cc_harness/memory/ 已有 __init__.py,看现有函数签名再改,不要覆盖现有逻辑)

- [ ] **Step 5: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_memory_tools.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: 加 L4 规则**

`cc_harness/policy.py` 加 2 规则(具体改法要读现有 policy.py 模式 — 看 `rbac` / `shell_ask` 怎么写的,照抄):

```python
# memory tool rules
TOOL_RULES.append(Rule(
    tool="memory_store",
    action=Action.ASK,
    reason="写记忆库需用户确认",
))

TOOL_RULES.append(Rule(
    tool="memory_query",
    action=Action.ALLOW,
    args_deny_patterns=[r"(?i)(password|secret|token|credential)"],
    reason="query 命中 secret 关键词拦",
))
```

(实际写时用 `grep -n "shell_ask\|Action.ASK" cc_harness/policy.py` 看现有模式)

- [ ] **Step 7: 跑 cc-harness 自带 test_agent.py 确认没回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -v --timeout 30`
Expected: PASS(没改 agent.py 主流程)

- [ ] **Step 8: commit**

```bash
cd /d/agent_learning/cc-harness
git add cc_harness/tools.py cc_harness/memory/ cc_harness/policy.py eval/locomo/tests/test_memory_tools.py
git commit -m "feat(cc-harness): 加 2 native tool (memory_store / memory_query) + L4 规则 (Task 4)"
```

---

## Task 5: cc_harness agent.py — run_turn_sync 同步外壳

**Files:**
- Modify: `cc_harness/agent.py`(抽 `_run_turn_inner` + 加 `run_turn_sync`)

- [ ] **Step 1: 写契约测试**

```python
# tests/test_agent_run_turn_sync.py(放仓根 tests/,跟 test_agent.py 同级)
import pytest
from cc_harness import agent

@pytest.mark.asyncio
async def test_run_turn_sync_matches_run_turn():
    """run_turn_sync(messages, options) should return same shape as async run_turn()."""
    # Mock messages + options to avoid real LLM call
    msgs = [{"role": "user", "content": "hi"}]
    opts = {"config": {"mode": "coding", "max_iter": 1}}
    sync_result = agent.run_turn_sync(msgs, opts)
    async_result = await agent.run_turn(msgs, opts)
    assert set(sync_result.keys()) == set(async_result.keys())
    assert "messages" in sync_result
    assert sync_result["messages"][-1]["role"] == "assistant"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent_run_turn_sync.py -v`
Expected: FAIL (run_turn_sync 不存在)

- [ ] **Step 3: 读 agent.py,看 run_turn 现状**

```bash
grep -n "def run_turn\|def _run\|async def" cc_harness/agent.py | head -10
```

(根据实际签名继续,以下为推荐改造)

- [ ] **Step 4: 重构 run_turn**

把原 `async def run_turn(messages, options) -> dict:` 内部逻辑包到 `def _run_turn_inner(messages, options) -> dict:`(同步核)。然后:

```python
async def run_turn(messages, options) -> dict:
    """REPL 用的 async 外壳,内部走同步核。"""
    return _run_turn_inner(messages, options)

def run_turn_sync(messages, options) -> dict:
    """locomo runner / 测试 用的同步入口。

    Returns:
        {
            "messages": [...],  # 含 assistant 终响应
            "result": "<结果段文本>",
            "iterations": int,
            "tool_calls": [{"name": ..., "args": ..., "result": ...}],
        }
    """
    return _run_turn_inner(messages, options)
```

**保证**:`_run_turn_inner` 跟原 run_turn 的核心逻辑 1:1(参数、return 字段不动),只把 async 关键字去掉。ReAct 循环的 `await llm_call` / `await tool_call` 改成同步版本或 `asyncio.run(coro)`。

- [ ] **Step 5: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent_run_turn_sync.py -v --timeout 30`
Expected: PASS

- [ ] **Step 6: 跑 cc-harness 全部 agent 测试,确认 REPL 行为不变**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_repl.py -v --timeout 60`
Expected: PASS(没回归)

- [ ] **Step 7: commit**

```bash
cd /d/agent_learning/cc-harness
git add cc_harness/agent.py tests/test_agent_run_turn_sync.py
git commit -m "refactor(cc-harness): 抽 _run_turn_inner + 加 run_turn_sync 同步外壳 (Task 5)

REPL 行为不变,只新增 sync 外壳供 locomo runner / 测试用。
原 run_turn 仍是 async,内部走 _run_turn_inner(同步核)。
契约测试:test_run_turn_sync_matches_run_turn。"
```

---

## Task 6: trace.py — langfuse SDK 封装

**Files:**
- Modify: `pyproject.toml`(加 `langfuse>=2.0`)
- Create: `eval/locomo/trace.py`

- [ ] **Step 1: 加依赖**

`pyproject.toml` `[project.dependencies]` 加:
```toml
"deepeval>=0.21",
"langfuse>=2.0",
```

然后: `pip install -e '.[dev]'` 装上

- [ ] **Step 2: 写测试(契约:无 langfuse client 时 fail-soft)**

```python
# eval/locomo/tests/test_trace.py
import os
from eval.locomo.trace import LocomoTrace

def test_trace_disabled_when_no_client(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    t = LocomoTrace("test-sample", enabled=True)
    assert t.enabled is False  # gracefully disabled

def test_trace_enabled_with_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    t = LocomoTrace("test-sample", enabled=True)
    assert t.enabled is True

def test_trace_force_disabled():
    t = LocomoTrace("test-sample", enabled=False)
    assert t.enabled is False
    # All methods should be no-ops
    span = t.start_turn(0, "hi")
    assert span is None
    t.record_llm(span, "model", [], "out", {})
    t.record_tool(span, "x", {}, {})
    t.score("f1", 0.5)
    t.flush()  # no-op, no error
```

- [ ] **Step 3: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_trace.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 trace.py**

```python
"""Langfuse SDK wrapper for locomo runner.

设计原则:
- 无 API key 时 enabled=False,所有方法是 no-op,不让 eval 挂
- enabled=False 时调任何方法不抛错(runner 跑 smoke 不需要 langfuse)
"""
from __future__ import annotations
import os
from typing import Any, Optional


class LocomoTrace:
    def __init__(self, sample_id: str, enabled: bool = True):
        self.sample_id = sample_id
        self._client = None
        if enabled:
            pk = os.getenv("LANGFUSE_PUBLIC_KEY")
            sk = os.getenv("LANGFUSE_SECRET_KEY")
            host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
            if pk and sk:
                try:
                    from langfuse import Langfuse
                    self._client = Langfuse(public_key=pk, secret_key=sk, host=host)
                except Exception:
                    self._client = None
        self.enabled = self._client is not None
        self._trace = None

    def _trace_or_skip(self):
        if not self.enabled:
            return None
        if self._trace is None:
            self._trace = self._client.trace(
                name=f"locomo-{self.sample_id}",
                user_id="cc-harness-locomo-runner",
            )
        return self._trace

    def start_turn(self, turn_idx: int, text: str):
        trace = self._trace_or_skip()
        if trace is None:
            return None
        return trace.span(name=f"turn-{turn_idx}", input=text)

    def record_llm(self, span, model: str, input_msgs: Any, output: Any, usage: dict):
        if span is None:
            return
        span.generation(
            name="llm-call",
            model=model,
            input=input_msgs,
            output=output,
            usage=usage or {},
        )

    def record_tool(self, span, name: str, args: dict, result: Any):
        if span is None:
            return
        span.event(name=f"tool-{name}", input=args, output=result)

    def score(self, name: str, value: float):
        trace = self._trace_or_skip()
        if trace is None:
            return
        try:
            trace.score(name=name, value=value)
        except Exception:
            pass

    def update(self, output: dict):
        trace = self._trace_or_skip()
        if trace is None:
            return
        try:
            trace.update(output=output)
        except Exception:
            pass

    def flush(self):
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:
            pass
```

- [ ] **Step 5: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_trace.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: commit**

```bash
cd /d/agent_learning/cc-harness
git add pyproject.toml eval/locomo/trace.py eval/locomo/tests/test_trace.py
git commit -m "feat(locomo-eval): trace.py langfuse SDK 封装 + 加 deepeval/langfuse 依赖 (Task 6)"
```

---

## Task 7: report.py — HTML 报告

**Files:**
- Create: `eval/locomo/report.py`

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_report.py
import json
from pathlib import Path
from eval.locomo.report import write_html_report, load_report_results

def test_write_html_report_creates_file(tmp_path):
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "single-hop", "status": "ok",
         "f1": 0.8, "quality": 0.9, "pass": True,
         "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.001,
         "tool_calls": ["memory_query"]},
        {"sample_id": "s1", "turn_idx": 1, "q_type": "multi-hop", "status": "timeout",
         "f1": None, "quality": None, "pass": False,
         "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
         "tool_calls": []},
    ]
    out = tmp_path / "report.html"
    write_html_report(results, out)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "locomo" in text.lower()
    assert "s1" in text
    assert "ok" in text
    assert "timeout" in text

def test_load_report_results_round_trip(tmp_path):
    src = tmp_path / "results.json"
    src.write_text(json.dumps([
        {"sample_id": "s1", "turn_idx": 0, "q_type": "x", "status": "ok",
         "f1": 0.5, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001,
         "tool_calls": []}
    ]))
    loaded = load_report_results(src)
    assert len(loaded) == 1
    assert loaded[0]["sample_id"] == "s1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 report.py**

(参考 `eval/promptfoo/tools/report_to_html.py` 现有 HTML 样式)

```python
"""HTML report for locomo eval results. 6-status schema + summary cards."""
from __future__ import annotations
import html
import json
from pathlib import Path
from typing import Iterable

STATUS_COLORS = {
    "ok": ("#3fb950", "#0d2818"),
    "quality_null": ("#d29922", "#2a1e08"),
    "agent_crash": ("#6e7681", "#1c1c1c"),
    "infra_fail": ("#6e7681", "#1c1c1c"),
    "timeout": ("#6e7681", "#1c1c1c"),
    "skipped": ("#6e7681", "#1c1c1c"),
}


def _summary_cards(results: list[dict]) -> str:
    n = len(results)
    n_pass = sum(1 for r in results if r.get("pass"))
    f1_vals = [r["f1"] for r in results if r.get("f1") is not None]
    quality_vals = [r["quality"] for r in results if r.get("quality") is not None]
    total_cost = sum(r.get("cost_usd") or 0 for r in results)
    total_tool_calls = sum(len(r.get("tool_calls") or []) for r in results)
    f1_med = sorted(f1_vals)[len(f1_vals) // 2] if f1_vals else 0
    q_med = sorted(quality_vals)[len(quality_vals) // 2] if quality_vals else 0
    cards = [
        ("pass", f"{n_pass}/{n} ({n_pass/n*100:.0f}%)" if n else "0"),
        ("f1-median", f"{f1_med:.3f}"),
        ("quality-median", f"{q_med:.3f}"),
        ("cost-usd", f"${total_cost:.4f}"),
        ("tool-calls", f"{total_tool_calls}"),
    ]
    out = ['<div class="cards">']
    for cls, val in cards:
        out.append(f'<div class="card {cls}"><div class="card-num">{val}</div><div class="card-lbl">{cls}</div></div>')
    out.append("</div>")
    return "\n".join(out)


def _row(r: dict) -> str:
    status = r.get("status", "ok")
    fg, bg = STATUS_COLORS.get(status, ("#fff", "#222"))
    cells = [
        r.get("sample_id", ""),
        r.get("turn_idx", ""),
        r.get("q_type", ""),
        f'<span style="color:{fg};background:{bg};padding:2px 6px;border-radius:3px">{status}</span>',
        f"{r.get('f1', ''):.3f}" if r.get("f1") is not None else "-",
        f"{r.get('quality', ''):.3f}" if r.get("quality") is not None else "-",
        "✓" if r.get("pass") else "✗",
        r.get("prompt_tokens", ""),
        r.get("completion_tokens", ""),
        f"${r.get('cost_usd', 0):.4f}",
        ", ".join(r.get("tool_calls") or []),
    ]
    return "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells) + "</tr>"


def write_html_report(results: list[dict], out_path: Path, title: str = "cc-harness locomo 评测报告") -> Path:
    """Write self-contained HTML report. Returns out_path."""
    rows = "\n".join(_row(r) for r in results)
    cards = _summary_cards(results)
    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       background: #0f1419; color: #e6edf3; margin: 0; padding: 24px; line-height: 1.5; }}
h1 {{ margin: 0 0 16px 0; font-size: 24px; }}
.cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 24px 0; }}
.card {{ flex: 1; min-width: 140px; background: #161b22; border: 1px solid #30363d;
        border-radius: 8px; padding: 12px; text-align: center; }}
.card-num {{ font-size: 20px; font-weight: 600; }}
.card-lbl {{ color: #7d8590; font-size: 12px; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #30363d; }}
th {{ background: #161b22; color: #7d8590; font-weight: 600; }}
tr:hover {{ background: #1c1c1c; }}
</style></head><body>
<h1>{title}</h1>
{cards}
<table>
<thead><tr>
<th>sample_id</th><th>turn</th><th>q_type</th><th>status</th>
<th>f1</th><th>quality</th><th>pass</th>
<th>prompt_tok</th><th>comp_tok</th><th>cost</th><th>tool_calls</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""
    out_path = Path(out_path)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def load_report_results(json_path: Path) -> list[dict]:
    return json.loads(Path(json_path).read_text(encoding="utf-8"))
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/report.py eval/locomo/tests/test_report.py
git commit -m "feat(locomo-eval): HTML 报告生成 + load_report_results (Task 7)"
```

---

## Task 8: runner.py — 主循环

**Files:**
- Create: `eval/locomo/runner.py`
- Modify: `.env.example`(加 3 LANGFUSE 项)
- Modify: `policy.yaml`(加 locomo_eval 段)

- [ ] **Step 1: 改 .env.example**

```
# Langfuse cloud(可选,无 key 时 trace 自动 graceful disable)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

- [ ] **Step 2: 加 policy.yaml 段**

```yaml
# policy.yaml 顶部加:
locomo_eval:
  enabled: true
  trace_to_langfuse: true
  max_turns_per_sample: 500
  sample_timeout_s: 1800
```

- [ ] **Step 3: 写 smoke 测试**

```python
# eval/locomo/tests/test_runner_smoke.py
import os
import subprocess
import sys
import time
from pathlib import Path

PY = ".venv/Scripts/python.exe"  # Windows; POSIX 用 "python"
REPO = Path(__file__).resolve().parents[3]


def test_runner_smoke_one_sample_no_trace(tmp_path):
    """--limit 1 --no-trace 必须能跑通(走 mock locomo data)。"""
    if not (REPO / "eval/locomo/data/locomo10.json").exists():
        import pytest
        pytest.skip("locomo10.json not downloaded; skip smoke")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, str(REPO / "eval/locomo/runner.py"), "--limit", "1", "--no-trace",
         "--output-dir", str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, f"runner failed: {proc.stderr[-500:]}"
    # Should produce HTML + results JSON
    html_files = list(tmp_path.glob("locomo-report-*.html"))
    json_files = list(tmp_path.glob("locomo-results-*.json"))
    assert html_files, f"no HTML report; stderr={proc.stderr[-500:]}"
    assert json_files, f"no results JSON; stderr={proc.stderr[-500:]}"
```

- [ ] **Step 4: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_runner_smoke.py -v --timeout 700`
Expected: FAIL(runner.py 不存在)

- [ ] **Step 5: 实现 runner.py**

```python
"""Locomo eval runner — replays 10 long conversations, scores QA, outputs HTML + langfuse trace.

Usage:
    python eval/locomo/runner.py                       # full 10 samples
    python eval/locomo/runner.py --limit 1 --no-trace  # smoke
    python eval/locomo/runner.py --resume              # from .checkpoint.json
    python eval/locomo/runner.py --eval-only           # re-score existing JSON
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from dotenv import dotenv_values
from eval.locomo import dataset as ds
from eval.locomo.evaluator import evaluate_qa
from eval.locomo.report import write_html_report
from eval.locomo.trace import LocomoTrace
from eval.locomo.download_dataset import verify_dataset, DEFAULT_FILE

CHECKPOINT = REPO / "eval/locomo/.checkpoint.json"


def _env():
    e = {**os.environ, **{k: v for k, v in dotenv_values(REPO / ".env").items() if v}}
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _load_policy():
    """Read locomo_eval: section from policy.yaml. Defaults to all-allowed."""
    p = REPO / "policy.yaml"
    if not p.exists():
        return {"enabled": True, "trace_to_langfuse": True, "max_turns_per_sample": 500, "sample_timeout_s": 1800}
    import yaml
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("locomo_eval", {})


def _make_messages_with_turn(text: str, speaker: str) -> list[dict]:
    """Convert a locomo turn into an initial messages list for run_turn_sync."""
    return [{"role": "user", "content": f"[{speaker}] {text}"}]


def run_sample(sample: dict, trace: LocomoTrace, max_turns: int, sample_timeout_s: int) -> tuple[list[dict], bool]:
    """Replay a single sample. Returns (results_per_qa, sample_fully_passed)."""
    from cc_harness import agent
    parsed = ds.parse_sample(sample)
    turns = list(ds.iter_turns(parsed))
    turns = turns[:max_turns]
    messages: list[dict] = []
    started = time.time()
    sample_failed = False

    for t in turns:
        if time.time() - started > sample_timeout_s:
            return [], False  # caller marks as timeout
        messages = _make_messages_with_turn(t.text, t.speaker)
        span = trace.start_turn(len(messages), t.text)
        try:
            out = agent.run_turn_sync(messages, {"config": {"mode": "coding", "max_iter": 8}})
        except Exception as e:
            sample_failed = True
            trace.record_tool(span, "agent_crash", {"err": str(e)[:200]}, {"ok": False})
            break
        # Update messages with full reply chain
        messages = out.get("messages", messages)
        # Record per-tool events
        for tc in (out.get("tool_calls") or []):
            trace.record_tool(span, tc.get("name", "?"), tc.get("args", {}), tc.get("result", {}))

    if sample_failed:
        return [], False

    # Now ask each QA
    results = []
    for qa in ds.iter_qa(parsed):
        qa_messages = list(messages) + [{"role": "user", "content": qa.question}]
        span = trace.start_turn(-1, qa.question)
        try:
            out = agent.run_turn_sync(qa_messages, {"config": {"mode": "coding", "max_iter": 6}})
            predicted = (out.get("messages") or [{}])[-1].get("content", "")
            for tc in (out.get("tool_calls") or []):
                trace.record_tool(span, tc.get("name", "?"), tc.get("args", {}), tc.get("result", {}))
        except Exception as e:
            results.append({
                "sample_id": parsed.sample_id, "turn_idx": -1, "q_type": qa.category,
                "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                "tool_calls": [], "error": str(e)[:200],
            })
            continue
        eval_result = evaluate_qa(qa.question, predicted, qa.answer)
        # token counts — proxy from out; deepeval 已有 usage 概念
        results.append({
            "sample_id": parsed.sample_id,
            "turn_idx": -1,
            "q_type": qa.category,
            "status": "ok" if eval_result["quality"] is not None else "quality_null",
            "f1": eval_result["f1"],
            "quality": eval_result["quality"],
            "pass": eval_result["pass"],
            "prompt_tokens": out.get("usage", {}).get("prompt_tokens", 0),
            "completion_tokens": out.get("usage", {}).get("completion_tokens", 0),
            "cost_usd": out.get("cost_usd", 0.0),
            "tool_calls": [tc.get("name") for tc in (out.get("tool_calls") or [])],
        })
        trace.score("f1", eval_result["f1"])
        trace.score("quality", eval_result["quality"] or 0.0)
        trace.update(eval_result["trace_payload"])
    return results, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--no-check-trace", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--output-dir", type=Path, default=REPO / "eval/result")
    args = ap.parse_args()

    policy = _load_policy()
    if not policy.get("enabled", True):
        print("[runner] locomo_eval disabled in policy.yaml; exit 0")
        return 0

    # Load dataset
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
    if json_path.exists() and not args.eval_only:
        all_results = json.loads(json_path.read_text(encoding="utf-8"))
        done = [r["sample_id"] for r in all_results]

    enabled_trace = (not args.no_trace) and policy.get("trace_to_langfuse", True)
    if not args.no_check_trace and enabled_trace:
        # quick env check
        if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
            print("[yellow]LANGFUSE_* env not set; trace will be no-op (graceful)")
            enabled_trace = False

    for sample in samples:
        print(f"[runner] sample {sample['sample_id']} ...", flush=True)
        trace = LocomoTrace(sample["sample_id"], enabled=enabled_trace)
        try:
            results, ok = run_sample(
                sample, trace,
                max_turns=policy.get("max_turns_per_sample", 500),
                sample_timeout_s=policy.get("sample_timeout_s", 1800),
            )
            if not ok and not results:
                results = [{"sample_id": sample["sample_id"], "turn_idx": -1, "q_type": "n/a",
                            "status": "timeout", "f1": None, "quality": None, "pass": False,
                            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                            "tool_calls": []}]
        except Exception as e:
            results = [{"sample_id": sample["sample_id"], "turn_idx": -1, "q_type": "n/a",
                        "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                        "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                        "tool_calls": [], "error": str(e)[:200]}]
        all_results.extend(results)
        done.append(sample["sample_id"])
        # Append per-sample (idempotent)
        json_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=1), encoding="utf-8")
        CHECKPOINT.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
        trace.flush()
        print(f"[runner]   {sample['sample_id']}: {len(results)} qa, "
              f"{sum(1 for r in results if r.get('pass'))} pass", flush=True)

    # Final HTML
    write_html_report(all_results, html_path)
    print(f"[runner] DONE. results: {json_path}  html: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 跑 smoke 测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_runner_smoke.py -v --timeout 700`
Expected: PASS(若 locomo10.json 已下载,需 ~5min 跑完 1 sample)

- [ ] **Step 7: 跑一次手动 smoke(确认 runner 真实能跑)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --no-check-trace --output-dir /tmp/locomo-smoke`
Expected: 跑完 1 sample,产出 HTML + JSON

- [ ] **Step 8: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/runner.py eval/locomo/tests/test_runner_smoke.py .env.example policy.yaml
git commit -m "feat(locomo-eval): runner.py 主循环 + policy.yaml kill-switch + .env.example (Task 8)"
```

---

## Task 9: 集成 — 全量 10 样本跑

**Files:**
- (no new files, this is the integration verification task)

- [ ] **Step 1: 清旧 checkpoint(隔离)**

```bash
rm -f eval/locomo/.checkpoint.json eval/result/locomo-report-*.html eval/result/locomo-results-*.json
```

- [ ] **Step 2: 跑 1 sample,实测 wall-clock,校准 sample_timeout**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --no-check-trace --output-dir /tmp/locomo-calib`
记录 `Duration:` 行(自己在输出里找)
Expected: ≤ sample_timeout_s(默认 30 min)

如果 1 sample 跑 5 min,校准:
- `policy.yaml` sample_timeout_s = 600(10 min,给足 buffer)
- 不够 → 拉到 1200(20 min)

- [ ] **Step 3: 跑全量 10 样本(走 langfuse cloud,真上报)**

确认 `.env` 里有 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`,然后:

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --output-dir eval/result`
Expected: ~10 × (calibrated sample time) — 估 30-60 min
产出 `eval/result/locomo-report-YYYY-MM-DD.html` + `locomo-results-YYYY-MM-DD.json`
langfuse cloud 项目 cc-harness-locomo 有 10 个 trace

- [ ] **Step 4: 验证 langfuse 上有 trace**

打开 https://cloud.langfuse.com → 项目 `cc-harness-locomo` → Traces tab
Expected: 10 个 trace,每个有 turn spans + llm generations + tool events + f1/quality score

- [ ] **Step 5: 看 HTML 报告(自查)**

打开 `eval/result/locomo-report-YYYY-MM-DD.html`
Expected: summary cards 显示总样本/通过/F1 中位数/质量中位数/总成本/总 tool 调用数。表格每条 QA 一行,status 颜色对应(绿=ok,黄=quality_null,灰=agent_crash/timeout/infra_fail/skipped)

- [ ] **Step 6: commit(报告不入仓,但 commit 复盘)**

```bash
cd /d/agent_learning/cc-harness
git add -A  # 加新 policy.yaml / .env.example 等
git status  # 确认无 data/ 或 .checkpoint.json 提交
git commit -m "chore(locomo-eval): 跑通全量 10 样本 + 校准 sample_timeout (Task 9)

不 commit data/locomo10.json(数据不入仓,已在 .gitignore)
不 commit eval/result/locomo-*.html / .json(本地报告,不入仓)"
```

(若 policy.yaml / .env.example 在前面 task 已 commit,这里只 commit 其它必要的)

---

## 验收 checklist(全部 ✓ 才算完成)

- [ ] `pytest eval/locomo/tests/ -v` 全 pass
- [ ] `pytest tests/test_agent.py tests/test_repl.py tests/test_agent_run_turn_sync.py -v --timeout 60` 无回归
- [ ] `python eval/locomo/runner.py --limit 1 --no-trace` 跑通 1 样本
- [ ] `python eval/locomo/runner.py` 跑通 10 样本,产出 `eval/result/locomo-report-YYYY-MM-DD.html`
- [ ] langfuse cloud 看到 10 个 trace
- [ ] `memory_store` / `memory_query` 走 L4 闸门(写 .git 操作日志 `logs/policy.jsonl`)
- [ ] 5 个 spec AC 全过(AC1-AC4 + AC5 报告可读)
