# Locomo 长对话记忆 QA 评测子系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 `eval/locomo/` 子系统,跑 snap-research/locomo 10 样本,测 cc-harness agent 长对话记忆 + 成本 + 任务轨迹,结果上 langfuse cloud + 出 HTML 报告。

**Architecture:** `eval/locomo/runner.py` 用 `asyncio.run` 调 `cc_harness.agent.run_turn(messages, llm, mcp, extra_native_specs=...)`,把 f3141b6 memory tools 注入(closure-based via `entry["deps"]`)。`cc_harness/memory/` 8 个文件从 git checkout 040e518~f3141b6 恢复。L4 引擎 `_classify` 加 2 case(`memory_save` → fs_write / `memory_recall` → fs_read)。deepeval 评质量(GEval)+ token F1(locomo 官方)。langfuse SDK 上 cloud,trace turn-level aggregate(runner 自报 token 数,因为 `run_turn` 不暴露 LLM call 回调)。

**Tech Stack:** Python 3.13、deepeval、langfuse、asyncio、pytest、locomo10(JSON,gitignored)、cc-harness 现有 ReAct + L4 引擎。

**Spec:** `docs/superpowers/specs/2026-07-07-locomo-eval-design.md` v2(2 轮 review approved, `e560f94`)

**Scope:** 单子项目,9 个 task,自包含 5 phase(数据/工具 wire/runner/测试/全量跑)。每个 task TDD:写测试 → 跑 fail → 实现 → 跑 pass → commit。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `eval/locomo/__init__.py` | 包 marker(空) |
| `eval/locomo/download_dataset.py` | 拉 locomo10.json 到 `eval/locomo/data/`,sanity-check |
| `eval/locomo/dataset.py` | locomo JSON 解析:`parse_sample` / `iter_turns` / `iter_qa` |
| `eval/locomo/evaluator.py` | `token_f1` + `quality_score`(deepeval GEval, fail-soft)+ `evaluate_qa` |
| `eval/locomo/trace.py` | langfuse SDK 封装:`LocomoTrace`(fail-soft 无 key) |
| `eval/locomo/report.py` | HTML 报告生成(6 状态 schema + summary cards) |
| `eval/locomo/runner.py` | 主循环(10 样本,replay + QA + 评分 + 报告 + langfuse) |
| `eval/locomo/policy_local.yaml` | locomo 子系统独立 kill-switch |
| `eval/locomo/tests/__init__.py` | pytest 包 marker |
| `eval/locomo/tests/test_evaluator.py` | `token_f1` + `evaluate_qa` 单测 |
| `eval/locomo/tests/test_dataset.py` | locomo 解析单测 |
| `eval/locomo/tests/test_runner_smoke.py` | 端到端 smoke `--no-memory-tools --no-trace` |
| `eval/locomo/data/.gitkeep` | 数据目录占位 |
| `eval/locomo/.gitignore` | 数据 + .checkpoint.json |
| `cc_harness/memory/` | **从 git checkout 恢复** 8 个文件(040e518~f3141b6) |
| `cc_harness/agent.py` | **小幅改**:加可选参数 `extra_native_specs: list[dict]`,每条 `{spec, handler, deps}` |
| `cc_harness/policy.py` | `_classify` 加 2 case(`memory_save` / `memory_recall`) |
| `tests/test_agent_run_turn_extra.py` | 契约测试:`extra_native_specs=None` 行为不变 |
| `tests/test_memory_tools_handler.py` | f3141b6 memory_recall/save handler 单测(放仓根 tests/) |
| `pyproject.toml` | 加 `deepeval>=0.21`、`langfuse>=2.0` 依赖 |
| `.env.example` | 加 LANGFUSE_* 3 项 |
| `.gitignore`(仓根) | 加 `eval/locomo/data/locomo10.json`、`eval/locomo/.checkpoint.json` |

---

## Task 1: 数据下载脚本 + sanity check

**Files:**
- Create: `eval/locomo/__init__.py`(空)
- Create: `eval/locomo/tests/__init__.py`(空)
- Create: `eval/locomo/download_dataset.py`
- Create: `eval/locomo/data/.gitkeep`(空)
- Create: `eval/locomo/.gitignore`

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_download_dataset.py
import json
from pathlib import Path
import pytest
from eval.locomo.download_dataset import verify_dataset

def test_verify_dataset_accepts_list(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps([
        {"sample_id": "s1", "conversation": {"session_1": [{"speaker": "A", "dia_id": "d1", "text": "hi"}]},
         "qa": [{"question": "q?", "answer": "a", "category": "test", "evidence": ["d1"]}]}
    ]))
    samples = verify_dataset(fake)
    assert len(samples) == 1
    assert samples[0]["sample_id"] == "s1"

def test_verify_dataset_accepts_single_dict(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps(
        {"sample_id": "x", "conversation": {}, "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}]}
    ))
    samples = verify_dataset(fake)
    assert len(samples) == 1

def test_verify_dataset_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="locomo data not found"):
        verify_dataset(tmp_path / "nonexistent.json")

def test_verify_dataset_rejects_no_qa(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps([{"sample_id": "x", "conversation": {}, "qa": []}]))
    with pytest.raises(ValueError, match="no QA pairs"):
        verify_dataset(fake)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_download_dataset.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 创建 marker 文件 + gitignore**

```bash
cd /d/agent_learning/cc-harness
touch eval/locomo/__init__.py eval/locomo/tests/__init__.py eval/locomo/data/.gitkeep
```

`eval/locomo/.gitignore`:
```
data/locomo10.json
.checkpoint.json
__pycache__/
*.pyc
```

- [ ] **Step 4: 实现 download_dataset.py**

```python
"""Download snap-research/locomo dataset to eval/locomo/data/.

Source: https://github.com/snap-research/locomo
File: data/locomo10.json (10 long conversations, with QA pairs).
License: see upstream LICENSE.txt — local eval only, NOT committed to this repo.
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
    Raises FileNotFoundError if file missing.
    Raises ValueError if file empty/no QA.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"locomo data not found at {path}; run download_dataset() first"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "sample_id" in raw:
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"locomo JSON must be list or sample dict, got {type(raw).__name__}")
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ValueError(f"sample #{i} is not a dict")
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

- [ ] **Step 5: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_download_dataset.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: 跑下载脚本(走网络,失败可接受)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/download_dataset.py`
Expected: `[download] OK: 10 samples, ...KB` 或网络失败(失败时手动 `curl -L https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json -o eval/locomo/data/locomo10.json`)

- [ ] **Step 7: 改仓根 .gitignore 加数据排除**

在 `/d/agent_learning/cc-harness/.gitignore` 加:
```
# locomo dataset (大,不入仓)
eval/locomo/data/locomo10.json
eval/locomo/.checkpoint.json
```

- [ ] **Step 8: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/ .gitignore
git commit -m "feat(locomo-eval): 数据下载脚本 + sanity check (Task 1)"
```

---

## Task 2: dataset.py + 单测

**Files:**
- Create: `eval/locomo/dataset.py`

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_dataset.py
from eval.locomo.dataset import parse_sample, iter_turns, iter_qa, Turn, QA

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
    assert isinstance(turns[0], Turn)
    assert turns[0].text == "hello"
    assert turns[0].session == "session_1"
    assert turns[2].session == "session_2"
    qa = list(iter_qa(sample))
    assert len(qa) == 1
    assert isinstance(qa[0], QA)
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
                {"bad": "entry"},
            ],
        },
        "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}],
    }
    sample = parse_sample(raw)
    turns = list(iter_turns(sample))
    assert len(turns) == 1
    assert turns[0].text == "ok"

def test_iter_qa_returns_empty_for_no_qa():
    raw = {"sample_id": "x", "conversation": {}, "qa": []}
    sample = parse_sample(raw)
    assert list(iter_qa(sample)) == []

def test_iter_qa_skips_non_dict():
    raw = {
        "sample_id": "x",
        "conversation": {},
        "qa": ["not a dict", {"question": "q", "answer": "a", "category": "c", "evidence": []}],
    }
    sample = parse_sample(raw)
    qa = list(iter_qa(sample))
    assert len(qa) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_dataset.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 dataset.py**

```python
"""Locomo dataset parsing.

A locomo sample:
  {sample_id, conversation: {session_name: [{speaker, dia_id, text}, ...]}, qa: [{question, answer, category, evidence}]}
"""
from dataclasses import dataclass


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
    evidence: list[str]


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


def iter_turns(sample: Sample):
    """Yield Turn in session_name order. Skip entries missing speaker/text."""
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


def iter_qa(sample: Sample):
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
Expected: PASS (5 tests)

- [ ] **Step 5: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/dataset.py eval/locomo/tests/test_dataset.py
git commit -m "feat(locomo-eval): dataset 解析模块 + 5 单测 (Task 2)"
```

---

## Task 3: 恢复 cc_harness/memory/ 包(git checkout + 验 import)

**Files:**
- Restore: `cc_harness/memory/{__init__.py,embedding.py,store.py,config.py,decider.py,service.py,retriever.py,pipeline.py,tools.py}`(从 git 040e518~f3141b6)
- Modify: 可能需 `cc_harness/memory/service.py` 加 `delete_by_tag`

- [ ] **Step 1: 看 memory 历史 commit 顺序**

Run: `cd /d/agent_learning/cc-harness && git log --oneline --reverse -- "cc_harness/memory/" 2>&1 | head -10`
Expected: 8 个 commit 按时间顺序,040e518(MemoryConfig)是最早,f3141b6(memory tools)是最晚

- [ ] **Step 2: checkout 8 个 commit 的 memory 目录**

```bash
cd /d/agent_learning/cc-harness
# 用 f3141b6 的目录(包含所有文件,因为是最后 commit)
git checkout f3141b6 -- cc_harness/memory/
ls cc_harness/memory/
```

Expected: `__init__.py embedding.py store.py config.py decider.py service.py retriever.py pipeline.py tools.py` 都出现

- [ ] **Step 3: 验 import**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "from cc_harness.memory.service import MemoryService; from cc_harness.memory.retriever import MemoryRetriever; from cc_harness.memory.tools import MEMORY_SAVE_SPEC, MEMORY_RECALL_SPEC, memory_save_handler, memory_recall_handler; print('OK')" 2>&1 | tail -20`
Expected: `OK` 或 ImportError(失败信息给后续 task 用)

如果 ImportError:
- 找缺哪个模块 → git checkout 那个 commit → 重复
- 若 f3141b6 单独 checkout 不够,改为 range:
  ```bash
  git checkout 040e518~f3141b6 -- cc_harness/memory/
  ```

- [ ] **Step 4: 验 MemoryService 有 delete_by_tag 方法 + 看 MemoryStore schema**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "from cc_harness.memory.service import MemoryService; import inspect; print('delete_by_tag' in dir(MemoryService))" 2>&1 | tail -3`
Expected: `True` 或 `False`

**先 inspect schema**:
```bash
grep -n "CREATE TABLE\|tags" cc_harness/memory/store.py 2>&1 | head -10
```

看 tags 是 JSON column / junction table / 别的。然后再写 delete_by_tag:

如果 `False` → 在 `cc_harness/memory/service.py` 加最小实现(根据实际 schema):
```python
def delete_by_tag(self, tag_pattern: str) -> int:
    """Delete all memories whose tags match the LIKE pattern. Returns row count."""
    # 实现要 match store.py 实际 schema(tags 是 JSON column 还是 junction table)
    # 若是 JSON:WHERE json_extract(tags, '$') LIKE ? 或 tags LIKE ?
    # 若是 junction:DELETE FROM memory_tags WHERE tag LIKE ? 然后级联删 memories
    with sqlite3.connect(self.store.db_path) as conn:
        cur = conn.execute(
            "DELETE FROM memories WHERE tags LIKE ?",  # 或对应 schema 的 SQL
            (tag_pattern,),
        )
        conn.commit()
        return cur.rowcount
```

(具体 schema 看 inspect 输出,实现要 match 实际数据表结构)

- [ ] **Step 5: commit**

```bash
cd /d/agent_learning/cc-harness
git add cc_harness/memory/
git commit -m "feat(memory): 恢复 cc_harness/memory/ 8 文件 (f3141b6 baseline, Task 3)"
```

如果 Step 4 加了 `delete_by_tag`:
```bash
git add cc_harness/memory/service.py
git commit -m "feat(memory): 加 MemoryService.delete_by_tag (locomo 隔离用)"
```

---

## Task 4: evaluator.py(token F1 + deepeval GEval)

**Files:**
- Create: `eval/locomo/evaluator.py`
- Modify: `pyproject.toml`(加 `deepeval>=0.21`)
- Run: `pip install -e '.[dev]'` 装依赖

- [ ] **Step 1: 加依赖**

`pyproject.toml` `[project.dependencies]` 加:
```toml
"deepeval>=0.21",
```

- [ ] **Step 2: 装依赖**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pip install -e '.[dev]' 2>&1 | tail -5`

- [ ] **Step 3: 写测试**

```python
# eval/locomo/tests/test_evaluator.py
from eval.locomo.evaluator import token_f1, evaluate_qa

def test_token_f1_exact_match():
    assert token_f1("hello world", "hello world") == 1.0

def test_token_f1_partial():
    score = token_f1("the cat sat", "the cat sat on a mat")
    assert 0.4 < score < 0.8

def test_token_f1_no_overlap():
    assert token_f1("apple banana", "cherry date") == 0.0

def test_token_f1_empty_predicted():
    assert token_f1("", "anything") == 0.0

def test_token_f1_empty_gold():
    assert token_f1("anything", "") == 0.0

def test_token_f1_handles_cjk():
    score = token_f1("我喜欢苹果", "我喜欢苹果和香蕉")
    assert score > 0.5

def test_evaluate_qa_returns_dict_with_expected_keys():
    result = evaluate_qa("What color?", "blue", "blue")
    assert set(result.keys()) >= {"f1", "quality", "pass", "trace_payload"}
    assert result["f1"] == 1.0
    assert result["pass"] is True
    # quality may be None if deepeval judge LLM not available — that's fail-soft
    assert result["quality"] is None or 0.0 <= result["quality"] <= 1.0
    assert result["trace_payload"]["f1"] == result["f1"]
    assert result["trace_payload"]["pass"] == result["pass"]

def test_evaluate_qa_fail_when_low_f1_and_no_quality():
    result = evaluate_qa("q", "completely wrong answer xyzzy", "the cat sat on the mat")
    assert result["f1"] < 0.3
    if result["quality"] is None:
        assert result["pass"] is False
```

- [ ] **Step 4: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator.py -v`
Expected: FAIL

- [ ] **Step 5: 实现 evaluator.py**

```python
"""QA evaluation: token F1 (locomo official) + deepeval GEval (subjective quality)."""
from __future__ import annotations
import re
from typing import Optional


def _tokenize(text: str) -> list[str]:
    """Whitespace + simple word splitting. Handles CJK by per-character split."""
    if not text:
        return []
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
        )
        case = LLMTestCase(input=prompt, actual_output=predicted, expected_output=gold)
        metric.measure(case)
        return float(metric.score) / 100.0
    except Exception:
        return None


def evaluate_qa(prompt: str, predicted: str, gold: str) -> dict:
    """Returns dict with f1, quality, pass, trace_payload."""
    f1 = token_f1(predicted, gold)
    quality = quality_score(prompt, predicted, gold)
    pass_ = (f1 > 0.5) or (quality is not None and quality > 0.7)
    return {
        "f1": f1,
        "quality": quality,
        "pass": pass_,
        "trace_payload": {"f1": f1, "quality": quality, "pass": pass_},
    }
```

- [ ] **Step 6: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_evaluator.py -v`
Expected: PASS (7 tests; quality may be None in test env if no judge LLM)

- [ ] **Step 7: commit**

```bash
cd /d/agent_learning/cc-harness
git add pyproject.toml eval/locomo/evaluator.py eval/locomo/tests/test_evaluator.py
git commit -m "feat(locomo-eval): evaluator (F1 + deepeval GEval, fail-soft) + deepeval 依赖 (Task 4)"
```

---

## Task 5: cc_harness/agent.py 加 `extra_native_specs` 可选参数

**Files:**
- Modify: `cc_harness/agent.py`(在 run_turn 签名加 `extra_native_specs=None`,dispatch loop 加 extra 路径)
- Create: `tests/test_agent_extra_specs.py`(契约测试)

- [ ] **Step 1: 写契约测试**

```python
# tests/test_agent_extra_specs.py(放仓根 tests/,跟 test_agent.py 同级)
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from cc_harness import agent
from cc_harness.tokens import TokenCounter


class _FakeEvent:
    def __init__(self, kind, text="", name=None, args=None, call_id=None):
        self.kind = kind
        self.text = text
        self.name = name
        self.arguments = args or {}
        self.call_id = call_id or "call_1"


def _make_fake_llm(events):
    """Async iterator that yields events."""
    class LLM:
        async def chat(self, messages, tools):
            for e in events:
                yield e
    return LLM()


def _make_fake_mcp():
    mcp = MagicMock()
    mcp.list_tools = MagicMock(return_value=[])
    return mcp


@pytest.mark.asyncio
async def test_extra_specs_none_unchanged_behavior():
    """传 extra_native_specs=None 时,行为跟不传一样(REPL 不受影响)。"""
    llm = _make_fake_llm([_FakeEvent("content", text="hi back")])
    mcp = _make_fake_mcp()
    messages = [{"role": "user", "content": "hi"}]
    stats = await agent.run_turn(messages, llm, mcp, max_iter=3, mode="coding", cwd="/tmp")
    assert isinstance(stats, type(agent.run_turn.__annotations__["return"]))  # TurnTokenStats


@pytest.mark.asyncio
async def test_extra_specs_dispatched_to_handler():
    """传 extra_native_specs 时,handler 被调,args + cwd + deps 都传。"""
    handler = AsyncMock(return_value="handler-output")
    handler_spec = {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "test",
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }
    extras = [{"spec": handler_spec, "handler": handler, "deps": {"retriever": "fake-retriever"}}]
    # LLM: 先 content,然后 tool_call,再 content
    events = [
        _FakeEvent("content", text="ok"),
        _FakeEvent("tool_call_done", name="my_tool", args={"x": "hello"}, call_id="c1"),
        _FakeEvent("content", text="done"),
    ]
    llm = _make_fake_llm(events)
    mcp = _make_fake_mcp()
    messages = [{"role": "user", "content": "test"}]
    await agent.run_turn(
        messages, llm, mcp, max_iter=3, mode="coding", cwd="/tmp",
        extra_native_specs=extras,
    )
    assert handler.await_count >= 1
    call = handler.await_args
    assert call.kwargs.get("cwd") == "/tmp"
    assert call.kwargs.get("retriever") == "fake-retriever"
    assert call.args[0] == {"x": "hello"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent_extra_specs.py -v`
Expected: FAIL(extra_native_specs 参数不存在)

- [ ] **Step 3: 读 agent.py,看 run_turn 内部 dispatch 段**

```bash
grep -n "def run_turn\|NATIVE_TOOLS\[p.name\]\|tool_call_done" cc_harness/agent.py | head -10
```

记录 dispatch 段行号(通常是 200-300 行的 ReAct loop 内)。

- [ ] **Step 4: 改 agent.py**

a) 在 `run_turn` 签名 `l5: L5Engine | None = None,` 后加:
```python
extra_native_specs: list[dict] | None = None,
```

b) 在 ReAct loop 里 dispatch tool_call 时(原来调用 `NATIVE_TOOLS[p.name]["handler"](args, cwd=str(cwd))` 那里),加 `extra_native_specs` 路径:

**两处必改**:
1. **tool_specs 累积**(让 LLM 看到 extra specs),在 `for native in NATIVE_TOOLS.values():` 之后:
   ```python
   for native in NATIVE_TOOLS.values():
       tool_specs.append(native["spec"])
   for entry in (extra_native_specs or []):
       tool_specs.append(entry["spec"])  # NEW
   ```

2. **dispatch tool_call**(让 handler 被调),原来调用 `NATIVE_TOOLS[p.name]["handler"](args, cwd=str(cwd))` 那里:
   ```python
   # 原:
   result = await NATIVE_TOOLS[p.name]["handler"](args, cwd=str(cwd))

   # 改:
   result = None
   if p.name in NATIVE_TOOLS:
       result = await NATIVE_TOOLS[p.name]["handler"](args, cwd=str(cwd))
   else:
       for entry in (extra_native_specs or []):
           if entry["spec"]["function"]["name"] == p.name:
               h_kwargs = {"cwd": str(cwd), **entry.get("deps", {})}
               result = await entry["handler"](args, **h_kwargs)
               break
       if result is None:
           # 未知工具,记错误并继续
           ...
   ```

(具体改法看 agent.py 实际 ReAct loop 结构;核心:加 extra 分支,保持 NATIVE_TOOLS 路径不变以保 REPL 行为 1:1)

c) ToolResult 处理:handler 返回 `ToolResult`,把 `.llm_text` 给后续 LLM context(看 agent.py 怎么用 NATIVE_TOOLS handler 的返回)

- [ ] **Step 5: 跑契约测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent_extra_specs.py -v --timeout 30`
Expected: PASS (2 tests)

- [ ] **Step 6: 跑 cc-harness 自带 agent 测试,确认无回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_repl.py -v --timeout 60`
Expected: PASS

- [ ] **Step 7: commit**

```bash
cd /d/agent_learning/cc-harness
git add cc_harness/agent.py tests/test_agent_extra_specs.py
git commit -m "feat(cc-harness): run_turn 加 extra_native_specs 可选参数 + 契约测试 (Task 5)

REPL 行为不变(NATIVE_TOOLS 路径优先);extra 分支供 locomo runner
注入 memory tools(handler + per-call deps 一起传,agent 拆 kwargs)。
契约测试:test_extra_specs_none_unchanged_behavior +
test_extra_specs_dispatched_to_handler。"
```

---

## Task 6: cc_harness/policy.py `_classify` 加 2 case

**Files:**
- Modify: `cc_harness/policy.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_policy_memory_classify.py(放仓根 tests/)
from cc_harness.policy import _classify

def test_memory_save_classified_as_fs_write():
    assert _classify("memory_save") == "fs_write"

def test_memory_recall_classified_as_fs_read():
    assert _classify("memory_recall") == "fs_read"

def test_existing_classifications_unchanged():
    """确认新 case 不破已有分类。"""
    assert _classify("run_command") == "shell"
    assert _classify("mcp__filesystem__read_file") == "fs_read"
    assert _classify("mcp__filesystem__write_file") == "fs_write"
    assert _classify("mcp__git__status") == "git_read"
    assert _classify("mcp__git__commit") == "git_write"
    assert _classify("mcp__context7__get_docs") == "docs"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy_memory_classify.py -v`
Expected: 部分 FAIL(memory_save/memory_recall 走 `unknown` 分支,因为没有匹配规则)

- [ ] **Step 3: 改 policy.py**

在 `_classify` 函数里、`run_command` 分支后加:
```python
def _classify(name: str) -> str:
    n = name.lower()
    if n == "run_command":
        return "shell"
    if n == "memory_save":
        return "fs_write"
    if n == "memory_recall":
        return "fs_read"
    # ... 原其它分支不动
```

(具体插入位置见 policy.py 现有 `_classify` 函数)

- [ ] **Step 4: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy_memory_classify.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: 跑 cc-harness 自带 policy 测试,确认无回归**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy.py -v --timeout 30`
Expected: PASS(若 tests/test_policy.py 不存在,跑 `tests/` 全部)

- [ ] **Step 6: commit**

```bash
cd /d/agent_learning/cc-harness
git add cc_harness/policy.py tests/test_policy_memory_classify.py
git commit -m "feat(cc-harness): policy._classify 加 memory_save/memory_recall case (Task 6)

- memory_save → fs_write → L4 ASK(写操作)
- memory_recall → fs_read → L4 ALLOW(工作区内读)
- secret 关键词不进 policy(破 '无 deny' 不变式);service.save 内部 scrub 留给后续 spec"
```

---

## Task 7: trace.py(langfuse SDK 封装)

**Files:**
- Modify: `pyproject.toml`(加 `langfuse>=2.0`)
- Create: `eval/locomo/trace.py`

- [ ] **Step 1: 加依赖**

`pyproject.toml` `[project.dependencies]` 加:
```toml
"langfuse>=2.0",
```

- [ ] **Step 2: 装**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pip install -e '.[dev]' 2>&1 | tail -5`

- [ ] **Step 3: 写测试**

```python
# eval/locomo/tests/test_trace.py
import os
import pytest
from eval.locomo.trace import LocomoTrace


def test_trace_disabled_when_no_client(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    t = LocomoTrace("test-sample", enabled=True)
    assert t.enabled is False

def test_trace_enabled_with_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    t = LocomoTrace("test-sample", enabled=True)
    assert t.enabled is True

def test_trace_force_disabled_all_methods_noop():
    t = LocomoTrace("test-sample", enabled=False)
    assert t.enabled is False
    span = t.start_turn(0, "hi")
    assert span is None
    t.record_llm(span, "model", [], "out", {"prompt": 10, "completion": 5})
    t.record_tool(span, "memory_recall", {"q": "x"}, {"hits": []})
    t.score("f1", 0.5)
    t.update({"f1": 0.5})
    t.flush()  # no-op, no error
```

- [ ] **Step 4: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_trace.py -v`
Expected: FAIL

- [ ] **Step 5: 实现 trace.py**

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
        """记 turn-level aggregate LLM usage(单次 LLM call 不记,因 run_turn 不暴露回调)。"""
        if span is None:
            return
        try:
            span.generation(
                name="llm-aggregate",
                model=model,
                input=input_msgs,
                output=output,
                usage=usage or {},
            )
        except Exception:
            pass

    def record_tool(self, span, name: str, args: dict, result: Any):
        if span is None:
            return
        try:
            span.event(name=f"tool-{name}", input=args, output=result)
        except Exception:
            pass

    def score(self, name: str, value: float):
        trace = self._trace_or_skip()
        if trace is None:
            return
        try:
            trace.score(name=name, value=value)
        except Exception:
            pass

    def update(self, output: dict):
        """给 trace 追加 output payload(spec §3.3 没列,但 runner.py 需要,加性扩展)。"""
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

- [ ] **Step 6: 跑测试确认 pass**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_trace.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: commit**

```bash
cd /d/agent_learning/cc-harness
git add pyproject.toml eval/locomo/trace.py eval/locomo/tests/test_trace.py
git commit -m "feat(locomo-eval): trace.py langfuse SDK 封装 + langfuse 依赖 (Task 7)"
```

---

## Task 8: report.py(HTML 报告)

**Files:**
- Create: `eval/locomo/report.py`

- [ ] **Step 1: 写测试**

```python
# eval/locomo/tests/test_report.py
from pathlib import Path
from eval.locomo.report import write_html_report, load_report_results


def test_write_html_report_creates_file(tmp_path):
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "single-hop", "status": "ok",
         "f1": 0.8, "quality": 0.9, "pass": True,
         "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.001,
         "tool_calls": ["memory_recall"]},
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


def test_summary_cards_appear(tmp_path):
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "x", "status": "ok",
         "f1": 0.5, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001,
         "tool_calls": ["memory_recall"]},
    ]
    out = tmp_path / "report.html"
    write_html_report(results, out)
    text = out.read_text(encoding="utf-8")
    # summary cards 用 class 名
    assert 'class="card' in text
    assert "f1-median" in text
    assert "cost-usd" in text


def test_load_report_results_round_trip(tmp_path):
    src = tmp_path / "results.json"
    src.write_text('[]', encoding="utf-8")
    assert load_report_results(src) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_report.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 report.py**

```python
"""HTML report for locomo eval results. 6-status schema + summary cards."""
from __future__ import annotations
import html
import json
from pathlib import Path

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
    f1_vals = sorted(r["f1"] for r in results if r.get("f1") is not None)
    quality_vals = sorted(r["quality"] for r in results if r.get("quality") is not None)
    total_cost = sum(r.get("cost_usd") or 0 for r in results)
    total_tool_calls = sum(len(r.get("tool_calls") or []) for r in results)
    f1_med = f1_vals[len(f1_vals) // 2] if f1_vals else 0.0
    q_med = quality_vals[len(quality_vals) // 2] if quality_vals else 0.0

    pass_label = f"{n_pass}/{n} ({n_pass/n*100:.0f}%)" if n else "0"
    cards = [
        ("pass", pass_label),
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
        str(r.get("sample_id", "")),
        str(r.get("turn_idx", "")),
        str(r.get("q_type", "")),
        f'<span style="color:{fg};background:{bg};padding:2px 6px;border-radius:3px">{status}</span>',
        f"{r.get('f1', ''):.3f}" if r.get("f1") is not None else "-",
        f"{r.get('quality', ''):.3f}" if r.get("quality") is not None else "-",
        "✓" if r.get("pass") else "✗",
        str(r.get("prompt_tokens", "")),
        str(r.get("completion_tokens", "")),
        f"${r.get('cost_usd', 0):.4f}",
        ", ".join(r.get("tool_calls") or []),
    ]
    return "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>"


def write_html_report(results: list[dict], out_path: Path,
                      title: str = "cc-harness locomo 评测报告") -> Path:
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
Expected: PASS (3 tests)

- [ ] **Step 5: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/report.py eval/locomo/tests/test_report.py
git commit -m "feat(locomo-eval): HTML 报告生成 (6 状态 + summary cards) + 3 单测 (Task 8)"
```

---

## Task 9: runner.py + policy_local.yaml + .env.example

**Files:**
- Create: `eval/locomo/runner.py`
- Create: `eval/locomo/policy_local.yaml`
- Modify: `.env.example`(加 3 LANGFUSE 项)

- [ ] **Step 1: 改 .env.example**

```
# Langfuse cloud(可选,无 key 时 trace 自动 graceful disable)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

- [ ] **Step 2: 创建 policy_local.yaml**

`eval/locomo/policy_local.yaml`:
```yaml
# Locomo 子系统独立 kill-switch(跟 cc-harness L4 解耦,仓根没 policy.yaml)
locomo_eval:
  enabled: true
  trace_to_langfuse: true
  max_turns_per_sample: 500
  sample_timeout_s: 1800
  inject_memory_tools: true
  clear_memory_tags: ["locomo/"]
```

- [ ] **Step 3: 写 smoke 测试**

```python
# eval/locomo/tests/test_runner_smoke.py
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY = ".venv/Scripts/python.exe"  # Windows; POSIX: "python"


def test_runner_smoke_no_memory_no_trace(tmp_path):
    """--limit 1 --no-trace --no-memory-tools 必须能跑通(不依赖 memory 包恢复 + langfuse)。"""
    if not (REPO / "eval/locomo/data/locomo10.json").exists():
        import pytest
        pytest.skip("locomo10.json not downloaded; run eval/locomo/download_dataset.py")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, str(REPO / "eval/locomo/runner.py"),
         "--limit", "1", "--no-trace", "--no-memory-tools",
         "--output-dir", str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"runner failed:\nSTDOUT={proc.stdout[-500:]}\nSTDERR={proc.stderr[-500:]}"
    html_files = list(tmp_path.glob("locomo-report-*.html"))
    json_files = list(tmp_path.glob("locomo-results-*.json"))
    assert html_files, f"no HTML report; stderr={proc.stderr[-500:]}"
    assert json_files, f"no results JSON; stderr={proc.stderr[-500:]}"
```

- [ ] **Step 4: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_runner_smoke.py -v --timeout 1000`
Expected: FAIL(runner.py 不存在)

- [ ] **Step 5: 实现 runner.py**

```python
"""Locomo eval runner — replays 10 long conversations, scores QA, outputs HTML + langfuse trace.

Usage:
    python eval/locomo/runner.py                          # full 10 samples
    python eval/locomo/runner.py --limit 1 --no-trace   # smoke
    python eval/locomo/runner.py --no-memory-tools       # baseline (no memory)
    python eval/locomo/runner.py --resume                 # from .checkpoint.json
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from dotenv import dotenv_values  # 已在 pyproject.toml(整个 cc-harness 都用,本 spec 不改)
from eval.locomo import dataset as ds
from eval.locomo.evaluator import evaluate_qa
from eval.locomo.report import write_html_report
from eval.locomo.trace import LocomoTrace
from eval.locomo.download_dataset import verify_dataset, DEFAULT_FILE

CHECKPOINT = REPO / "eval/locomo/.checkpoint.json"
POLICY_LOCAL = REPO / "eval/locomo/policy_local.yaml"


def _env():
    e = {**os.environ, **{k: v for k, v in dotenv_values(REPO / ".env").items() if v}}
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _load_policy():
    """Read policy_local.yaml (locomo subsystem). Default to all-allowed."""
    if not POLICY_LOCAL.exists():
        return {"enabled": True, "trace_to_langfuse": True, "max_turns_per_sample": 500,
                "sample_timeout_s": 1800, "inject_memory_tools": True,
                "clear_memory_tags": ["locomo/"]}
    import yaml
    return (yaml.safe_load(POLICY_LOCAL.read_text(encoding="utf-8")) or {}).get("locomo_eval", {})


def _make_initial_messages(turn_text: str, speaker: str) -> list[dict]:
    """Convert locomo turn to initial messages list."""
    return [{"role": "user", "content": f"[{speaker}] {turn_text}"}]


async def _build_memory_extras(policy: dict):
    """Build extra_native_specs with memory tools if enabled. Returns ([...extras], deps_or_None)."""
    if not policy.get("inject_memory_tools", True):
        return [], None
    try:
        from cc_harness.memory.store import MemoryStore
        from cc_harness.memory.embedding import EmbeddingClient
        from cc_harness.memory.decider import LLMDecider
        from cc_harness.memory.retriever import MemoryRetriever
        from cc_harness.memory.service import MemoryService
        from cc_harness.memory.tools import (
            MEMORY_RECALL_SPEC, MEMORY_SAVE_SPEC,
            memory_recall_handler, memory_save_handler,
        )
    except ImportError as e:
        print(f"[runner] memory import failed: {e}; running without memory tools")
        return [], None

    # 构造依赖(看 f3141b6 各自类的 __init__ 签名,以下为典型构造)
    try:
        # 从 .env 取 EMBEDDING_* 配置;若缺,降级到 no-op
        store = MemoryStore.from_env() if hasattr(MemoryStore, "from_env") else MemoryStore()
        embedder = EmbeddingClient.from_env() if hasattr(EmbeddingClient, "from_env") else EmbeddingClient()
        decider = LLMDecider.from_env() if hasattr(LLMDecider, "from_env") else LLMDecider()
        service = MemoryService(store=store, embedder=embedder, decider=decider)
        retriever = MemoryRetriever(store=store, embedder=embedder)
    except Exception as e:
        print(f"[runner] memory service init failed: {e}; running without memory tools")
        return [], None

    extras = [
        {"spec": MEMORY_RECALL_SPEC, "handler": memory_recall_handler, "deps": {"retriever": retriever}},
        {"spec": MEMORY_SAVE_SPEC, "handler": memory_save_handler, "deps": {"service": service}},
    ]
    return extras, {"service": service, "retriever": retriever}


def _clear_memory_tags(tags: list[str]):
    """Delete memories matching tag patterns (locomo isolation)."""
    if not tags:
        return
    try:
        from cc_harness.memory.service import MemoryService
        service = MemoryService.from_env() if hasattr(MemoryService, "from_env") else MemoryService()
        for tag in tags:
            try:
                n = service.delete_by_tag(tag)
                print(f"[runner] cleared {n} memories with tag '{tag}'")
            except Exception as e:
                print(f"[runner] clear tag '{tag}' failed: {e}")
    except ImportError:
        print("[runner] memory not available; skip tag clear")


async def _run_sample(sample: dict, policy: dict, extras: list[dict], trace: LocomoTrace) -> list[dict]:
    """Replay a single sample. Returns list of per-QA result dicts."""
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.agent import run_turn

    parsed = ds.parse_sample(sample)
    turns = list(ds.iter_turns(parsed))[: policy.get("max_turns_per_sample", 500)]
    started = time.time()
    sample_timeout_s = policy.get("sample_timeout_s", 1800)

    # Construct LLM + MCP
    llm = LLMClient.from_env() if hasattr(LLMClient, "from_env") else LLMClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
        base_url=os.environ["OPENAI_BASE_URL"],
    )
    mcp = MCPClient([])  # locomo 不需要 MCP 工具
    await mcp.start()

    try:
        messages: list[dict] = []  # 累积全对话;run_turn mutate in place
        for turn_idx, turn in enumerate(turns):
            if time.time() - started > sample_timeout_s:
                return [{"sample_id": parsed.sample_id, "turn_idx": -1, "q_type": "n/a",
                         "status": "timeout", "f1": None, "quality": None, "pass": False,
                         "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                         "tool_calls": []}]
            span = trace.start_turn(turn_idx, turn.text)
            # 追加新 turn 到累积 messages(不覆盖)
            messages.append({"role": "user", "content": f"[{turn.speaker}] {turn.text}"})
            try:
                stats = await run_turn(
                    messages, llm, mcp,
                    extra_native_specs=extras,
                    max_iter=8, mode="coding", cwd=str(REPO),
                )
            except Exception as e:
                trace.record_tool(span, "agent_crash", {"err": str(e)[:200]}, {"ok": False})
                # agent_crash: sample 剩余 QA 全标 agent_crash
                remaining = list(ds.iter_qa(parsed))
                return [{"sample_id": parsed.sample_id, "turn_idx": -1, "q_type": q.category,
                         "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                         "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                         "tool_calls": []} for q in remaining]

            # Record aggregate LLM usage for this turn
            trace.record_llm(span, os.environ.get("OPENAI_MODEL", "?"),
                             messages, stats, {"prompt_tokens": stats.api_prompt_tokens,
                                               "completion_tokens": stats.api_completion_tokens})

        # Ask each QA(基于累积的 messages,带全对话上下文)
        results = []
        for qa in ds.iter_qa(parsed):
            qa_messages = list(messages) + [{"role": "user", "content": qa.question}]
            span = trace.start_turn(-1, qa.question)
            try:
                stats = await run_turn(
                    qa_messages, llm, mcp,
                    extra_native_specs=extras,
                    max_iter=6, mode="coding", cwd=str(REPO),
                )
                predicted = qa_messages[-1].get("content", "")
                trace.record_llm(span, os.environ.get("OPENAI_MODEL", "?"),
                                 qa_messages, stats, {"prompt_tokens": stats.api_prompt_tokens,
                                                       "completion_tokens": stats.api_completion_tokens})
            except Exception as e:
                results.append({
                    "sample_id": parsed.sample_id, "turn_idx": -1, "q_type": qa.category,
                    "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                    "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                    "tool_calls": [], "error": str(e)[:200],
                })
                continue

            eval_result = evaluate_qa(qa.question, predicted, qa.answer)
            cost_usd = _estimate_cost(stats.api_prompt_tokens, stats.api_completion_tokens)
            results.append({
                "sample_id": parsed.sample_id,
                "turn_idx": -1,
                "q_type": qa.category,
                "status": "ok" if eval_result["quality"] is not None else "quality_null",
                "f1": eval_result["f1"],
                "quality": eval_result["quality"],
                "pass": eval_result["pass"],
                "prompt_tokens": stats.api_prompt_tokens,
                "completion_tokens": stats.api_completion_tokens,
                "cost_usd": cost_usd,
                "tool_calls": [],  # TODO: hook tool_calls from run_turn return
            })
            trace.score("f1", eval_result["f1"])
            if eval_result["quality"] is not None:
                trace.score("quality", eval_result["quality"])
            trace.update(eval_result["trace_payload"])
        return results
    finally:
        await mcp.stop()


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Rough cost estimate (DeepSeek pricing). Override via env if needed."""
    # DeepSeek v3: $0.14/M in, $0.28/M out (as of 2026-07)
    in_rate = float(os.environ.get("LOCOMO_COST_IN", "0.14")) / 1_000_000
    out_rate = float(os.environ.get("LOCOMO_COST_OUT", "0.28")) / 1_000_000
    return prompt_tokens * in_rate + completion_tokens * out_rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--no-check-trace", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-memory-tools", action="store_true")
    ap.add_argument("--output-dir", type=Path, default=REPO / "eval/result")
    args = ap.parse_args()

    policy = _load_policy()
    if not policy.get("enabled", True):
        print("[runner] locomo_eval disabled in policy_local.yaml; exit 0")
        return 0

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
    if json_path.exists() and not args.resume:
        all_results = json.loads(json_path.read_text(encoding="utf-8"))

    # Pre-warm: clear old memory tags (isolation)
    if not args.no_memory_tools:
        _clear_memory_tags(policy.get("clear_memory_tags", ["locomo/"]))

    enabled_trace = (not args.no_trace) and policy.get("trace_to_langfuse", True)
    if not args.no_check_trace and enabled_trace:
        if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
            print("[yellow]LANGFUSE_* env not set; trace will be no-op (graceful)")
            enabled_trace = False

    inject_memory = (not args.no_memory_tools) and policy.get("inject_memory_tools", True)

    async def amain():
        nonlocal all_results, done
        extras, mem_deps = await _build_memory_extras(
            {**policy, "inject_memory_tools": inject_memory}
        )

        for sample in samples:
            print(f"[runner] sample {sample['sample_id']} ...", flush=True)
            trace = LocomoTrace(sample["sample_id"], enabled=enabled_trace)
            try:
                results = await _run_sample(sample, policy, extras, trace)
            except Exception as e:
                results = [{"sample_id": sample["sample_id"], "turn_idx": -1, "q_type": "n/a",
                            "status": "agent_crash", "f1": None, "quality": None, "pass": False,
                            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                            "tool_calls": [], "error": str(e)[:200]}]
            all_results.extend(results)
            done.append(sample["sample_id"])
            json_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=1), encoding="utf-8")
            CHECKPOINT.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
            trace.flush()
            n_pass = sum(1 for r in results if r.get("pass"))
            print(f"[runner]   {sample['sample_id']}: {len(results)} qa, {n_pass} pass", flush=True)

    asyncio.run(amain())
    write_html_report(all_results, html_path)
    print(f"[runner] DONE. results: {json_path}  html: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 跑 smoke 测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest eval/locomo/tests/test_runner_smoke.py -v --timeout 1000`
Expected: PASS(若 locomo10.json 已下载,约 5-10 min)

- [ ] **Step 7: commit**

```bash
cd /d/agent_learning/cc-harness
git add eval/locomo/runner.py eval/locomo/tests/test_runner_smoke.py eval/locomo/policy_local.yaml .env.example
git commit -m "feat(locomo-eval): runner.py 主循环 + policy_local.yaml + smoke 测试 (Task 9)"
```

---

## 验收 checklist(全部 ✓ 才算完成)

- [ ] `pytest eval/locomo/tests/ -v` 4 单测文件全 pass(test_download_dataset + test_dataset + test_evaluator + test_trace + test_report + test_runner_smoke = 至少 17 测试)
- [ ] `pytest tests/test_agent.py tests/test_repl.py tests/test_agent_extra_specs.py tests/test_policy_memory_classify.py -v --timeout 60` 无回归
- [ ] `python eval/locomo/runner.py --limit 1 --no-trace --no-memory-tools` 跑通 1 样本 baseline
- [ ] `python eval/locomo/runner.py --limit 1 --no-trace` 跑通 1 样本带 memory tools(前提:Task 3 memory 包恢复 OK)
- [ ] `python eval/locomo/runner.py` 跑通 10 样本,产出 `eval/result/locomo-report-YYYY-MM-DD.html`
- [ ] langfuse cloud 看到 N 个 trace(若设了 key)
- [ ] `policy.jsonl` 有 `memory_save` ask 决策(若 memory tools 触发)
- [ ] 4 个 spec AC 全过(AC1-AC4)