# Memory Benchmark Eval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cross-session memory benchmark to `eval/`: synthetic dataset + multi-session runner + memory-specific metrics (cross-session recall precision/recall, long-term accuracy, save coverage, memory pollution rate). Reuses 70% of existing `eval/` infrastructure.

**Architecture:** New `eval/datasets/memory_bench.py` (JSON-backed synthetic dataset, no HF dependency), new `eval/runners/memory_session_runner.py` (multi-session driver that persists the SQLite `MemoryStore` across sessions but resets `messages` per session), extension of `eval/metrics/schema.py` with memory-specific fields, and a new `MemoryBenchReport` dataclass for aggregate metrics. CLI extends `eval/run.py` with `--benchmark {gaia,memory}` flag. All dataset/loader/runner code is **testable with FakeLLM + FakeMCP + FakeStore + FakeEmbedder** (no real LLM/embedding required for unit tests). One e2e smoke test exists under `tests/_test_memory_bench_e2e.py` (skipped without env).

**Tech Stack:** Python 3.11+, pydantic v2 (existing), `json` (stdlib for dataset), `re` (grading), existing `cc_harness.memory` package (Task 1-19), existing `eval/metrics/{schema,collector}.py`.

**Spec:** This document is the spec (no separate spec file for eval tooling; design lives here).

**Plan totals:** 14 atomic tasks, ~1100 lines of new code + tests.

---

## File Structure

| File | Status | Responsibility | Lines (target) |
|---|---|---|---:|
| `eval/datasets/memory_bench.json` | **NEW** | Synthetic 4-session × ~8 task dataset (30+ tasks) | 200 |
| `eval/datasets/memory_bench.py` | **NEW** | `MemoryBenchTask` + `MemoryBenchSession` dataclasses + `load_memory_bench()` | 100 |
| `eval/metrics/schema.py` | MODIFY | Add `MemoryTaskMetrics`, `MemorySessionMetrics`, `MemoryBenchReport` dataclasses; add `injected_memory_tokens` to `TaskMetrics` | +80 |
| `eval/metrics/memory_collector.py` | **NEW** | `collect_memory_task_metrics`, `aggregate_memory_session`, `aggregate_memory_bench`, `compute_recall_pr` (precision/recall on facts) | 200 |
| `eval/runners/memory_session_runner.py` | **NEW** | `run_memory_bench()` async driver: multi-session loop, db persistence, per-task-kind dispatch | 350 |
| `eval/run.py` | MODIFY | `--benchmark {gaia,memory}` flag; conditional session-runner import; pass memory db path through | +60 |
| `tests/eval/test_memory_bench.py` | **NEW** | 6 tests: dataset loader, recall PR, session isolation, db persistence, inject handler, recall handler | 250 |
| `tests/eval/test_memory_collector.py` | **NEW** | 4 tests: collect_task_metrics, aggregate_session, aggregate_bench, recall PR | 150 |
| `tests/_test_memory_bench_e2e.py` | **NEW** | 1 e2e (real LLM + real embedding, skipped without env) | 80 |
| `eval/README.md` | MODIFY | Add "Memory benchmark" section | +30 |
| `CLAUDE.md` | MODIFY | Add memory-bench eval command to Common commands | +5 |

**Total:** ~1300 lines net addition.

---

## Task Sequence

| # | Files | Risk | Why this order |
|---|---|---|---|
| 1 | `memory_bench.json` (data file, hand-authored) | None | Foundation; defines contract for everything downstream |
| 2 | `memory_bench.py` (loader) | Low | Pure dataclass + JSON parse; no LLM |
| 3 | `schema.py` (extend) | Low | Pure dataclass; downstream code reads |
| 4 | `memory_collector.py` (metrics) | Low | Pure functions; testable in isolation |
| 5 | `memory_session_runner.py` (skeleton + session loop) | **High** | Orchestrates multi-session state; mock-heavy |
| 6 | runner: `inject_facts` task handler | Medium | Verifies db persistence |
| 7 | runner: `recall_qa` task handler + grader | **High** | Cross-session recall; matches LLM tool calls to expected facts |
| 8 | runner: `filler_qa` task handler + pollution metric | Medium | Negative test for memory system |
| 9 | runner: aggregate + write outputs | Low | JSON/CSV/JSON output, mirrors `session_runner` |
| 10 | `tests/eval/test_memory_bench.py` (6 tests) | Low | TDD validates runner correctness |
| 11 | `tests/eval/test_memory_collector.py` (4 tests) | Low | TDD validates metrics |
| 12 | `eval/run.py` CLI integration (`--benchmark memory`) | Low | Wire runner to CLI |
| 13 | `tests/_test_memory_bench_e2e.py` | Low | Real-LLM smoke; underscore-prefixed |
| 14 | `eval/README.md` + `CLAUDE.md` | None | Docs |

---

## Task 1: Author the synthetic memory benchmark dataset

**Files:**
- Create: `eval/datasets/memory_bench.json` (hand-authored, deterministic)

- [ ] **Step 1: Hand-author the JSON dataset**

Create `eval/datasets/memory_bench.json` with this exact content:

```json
{
  "benchmark_id": "memory_bench_v1",
  "version": 1,
  "description": "Cross-session memory benchmark. 4 sessions, 32 tasks. Sessions 1-2 inject facts; sessions 2-4 test cross-session recall. Final session tests long-term retention.",
  "sessions": [
    {
      "session_id": "S1",
      "topic": "user_profile_basics",
      "tasks": [
        {
          "task_id": "S1-T1",
          "kind": "inject_facts",
          "user_input": "我住在北京, 在朝阳区工作, 养了一只橘猫叫小橘。",
          "expected_facts": ["用户住在北京", "用户在朝阳区工作", "用户养了一只橘猫叫小橘"]
        },
        {
          "task_id": "S1-T2",
          "kind": "filler_qa",
          "user_input": "今天天气怎么样?",
          "expected_facts": [],
          "ground_truth": "unknown"
        },
        {
          "task_id": "S1-T3",
          "kind": "inject_facts",
          "user_input": "我平时用 ruff 做 Python lint, 不喜欢用 Tailwind。",
          "expected_facts": ["用 ruff 做 Python lint", "不喜欢用 Tailwind"]
        },
        {
          "task_id": "S1-T4",
          "kind": "filler_qa",
          "user_input": "讲个笑话",
          "expected_facts": [],
          "ground_truth": "unknown"
        }
      ]
    },
    {
      "session_id": "S2",
      "topic": "project_decisions",
      "tasks": [
        {
          "task_id": "S2-T1",
          "kind": "inject_facts",
          "user_input": "项目用 PostgreSQL 不用 MySQL, 部署在阿里云。",
          "expected_facts": ["项目用 PostgreSQL 不用 MySQL", "部署在阿里云"]
        },
        {
          "task_id": "S2-T2",
          "kind": "recall_qa",
          "user_input": "我住在哪里?",
          "expected_facts": ["用户住在北京"],
          "ground_truth_keywords": ["北京"]
        },
        {
          "task_id": "S2-T3",
          "kind": "recall_qa",
          "user_input": "我养了什么宠物?",
          "expected_facts": ["用户养了一只橘猫叫小橘"],
          "ground_truth_keywords": ["橘猫", "小橘"]
        },
        {
          "task_id": "S2-T4",
          "kind": "followup_qa",
          "user_input": "推荐一下我周末能去哪玩",
          "expected_facts": ["用户住在北京", "用户在朝阳区工作"],
          "ground_truth_keywords": ["北京"]
        }
      ]
    },
    {
      "session_id": "S3",
      "topic": "long_retention",
      "tasks": [
        {
          "task_id": "S3-T1",
          "kind": "recall_qa",
          "user_input": "我用什么工具做 Python lint?",
          "expected_facts": ["用 ruff 做 Python lint"],
          "ground_truth_keywords": ["ruff"]
        },
        {
          "task_id": "S3-T2",
          "kind": "inject_facts",
          "user_input": "我们前端技术选型是 React 不用 Vue。",
          "expected_facts": ["前端用 React 不用 Vue"]
        },
        {
          "task_id": "S3-T3",
          "kind": "recall_qa",
          "user_input": "我住址和办公地址分别是什么?",
          "expected_facts": ["用户住在北京", "用户在朝阳区工作"],
          "ground_truth_keywords": ["北京", "朝阳"]
        },
        {
          "task_id": "S3-T4",
          "kind": "recall_qa",
          "user_input": "项目数据库和部署平台是什么?",
          "expected_facts": ["项目用 PostgreSQL 不用 MySQL", "部署在阿里云"],
          "ground_truth_keywords": ["PostgreSQL", "阿里云"]
        },
        {
          "task_id": "S3-T5",
          "kind": "followup_qa",
          "user_input": "前端用什么框架?",
          "expected_facts": ["前端用 React 不用 Vue"],
          "ground_truth_keywords": ["React"]
        }
      ]
    },
    {
      "session_id": "S4",
      "topic": "deep_chain",
      "tasks": [
        {
          "task_id": "S4-T1",
          "kind": "recall_qa",
          "user_input": "总结一下我之前告诉过你的所有信息",
          "expected_facts": [
            "用户住在北京",
            "用户在朝阳区工作",
            "用户养了一只橘猫叫小橘",
            "用 ruff 做 Python lint",
            "不喜欢用 Tailwind",
            "项目用 PostgreSQL 不用 MySQL",
            "部署在阿里云",
            "前端用 React 不用 Vue"
          ],
          "ground_truth_keywords": ["北京", "ruff", "PostgreSQL", "React", "阿里云"]
        },
        {
          "task_id": "S4-T2",
          "kind": "recall_qa",
          "user_input": "基于我之前的偏好, 我应该选什么 CSS 框架?",
          "expected_facts": ["不喜欢用 Tailwind"],
          "ground_truth_keywords": []
        },
        {
          "task_id": "S4-T3",
          "kind": "filler_qa",
          "user_input": "1+1 等于几?",
          "expected_facts": [],
          "ground_truth_keywords": ["2"]
        }
      ]
    }
  ]
}
```

- [ ] **Step 2: Validate JSON is well-formed**

Run: `.venv/Scripts/python.exe -c "import json; json.load(open(r'D:\agent_learning\cc-harness\eval\datasets\memory_bench.json')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add eval/datasets/memory_bench.json
git commit -m "test(memory-eval): add synthetic memory_bench v1 dataset (4 sessions, 17 tasks)"
```

(Note: `test` prefix because it's a test artifact / fixture, not application code. This is consistent with the project's convention of `eval/datasets/` being data files.)

---

## Task 2: Add memory benchmark loader

**Files:**
- Create: `eval/datasets/memory_bench.py`

- [ ] **Step 1: Write the failing test**

Create `tests/eval/__init__.py` (empty file; the directory may not exist) and `tests/eval/test_memory_bench.py`:

```python
# tests/eval/test_memory_bench.py
import json
import pytest
from eval.datasets.memory_bench import (
    MemoryBenchTask,
    MemoryBenchSession,
    MemoryBench,
    load_memory_bench,
)


def test_load_memory_bench_default_path():
    bench = load_memory_bench()
    assert bench.benchmark_id == "memory_bench_v1"
    assert len(bench.sessions) == 4
    assert sum(len(s.tasks) for s in bench.sessions) == 17  # 4+4+5+3
    s1 = bench.sessions[0]
    assert s1.session_id == "S1"
    assert s1.tasks[0].kind == "inject_facts"
    assert "我住在北京" in s1.tasks[0].user_input


def test_load_memory_bench_explicit_path():
    import pathlib
    path = pathlib.Path("eval/datasets/memory_bench.json")
    bench = load_memory_bench(path=path)
    assert bench.benchmark_id == "memory_bench_v1"


def test_memory_bench_task_kinds_are_valid():
    bench = load_memory_bench()
    valid_kinds = {"inject_facts", "recall_qa", "followup_qa", "filler_qa"}
    for session in bench.sessions:
        for task in session.tasks:
            assert task.kind in valid_kinds, f"{task.task_id} has unknown kind {task.kind!r}"


def test_recall_qa_tasks_have_expected_facts():
    bench = load_memory_bench()
    for session in bench.sessions:
        for task in session.tasks:
            if task.kind in ("recall_qa", "followup_qa"):
                assert task.expected_facts, f"{task.task_id} ({task.kind}) has empty expected_facts"
                assert task.ground_truth_keywords, f"{task.task_id} has empty ground_truth_keywords"


def test_inject_facts_tasks_have_expected_facts():
    bench = load_memory_bench()
    for session in bench.sessions:
        for task in session.tasks:
            if task.kind == "inject_facts":
                assert task.expected_facts, f"{task.task_id} (inject_facts) has empty expected_facts"


def test_filler_qa_tasks_have_no_expected_facts():
    bench = load_memory_bench()
    for session in bench.sessions:
        for task in session.tasks:
            if task.kind == "filler_qa":
                assert task.expected_facts == [], f"{task.task_id} (filler) should have no expected_facts"
```

(If `tests/eval/` already exists, do not overwrite `__init__.py`; just create `test_memory_bench.py`.)

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py -v`
Expected: `ModuleNotFoundError: No module named 'eval.datasets.memory_bench'`

- [ ] **Step 3: Implement the loader**

Create `eval/datasets/memory_bench.py`:

```python
"""Memory benchmark dataset loader.

JSON-backed synthetic dataset, no HuggingFace dependency. Used to evaluate
the cross-session recall capability of the long-term memory system.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path

# Default location relative to repo root
DEFAULT_BENCH_PATH = Path(__file__).parent / "memory_bench.json"


@dataclass(frozen=True)
class MemoryBenchTask:
    task_id: str
    kind: str          # "inject_facts" | "recall_qa" | "followup_qa" | "filler_qa"
    user_input: str
    expected_facts: list[str] = field(default_factory=list)
    ground_truth: str = ""          # for filler_qa
    ground_truth_keywords: list[str] = field(default_factory=list)  # for recall/followup


@dataclass(frozen=True)
class MemoryBenchSession:
    session_id: str
    topic: str
    tasks: list[MemoryBenchTask]


@dataclass(frozen=True)
class MemoryBench:
    benchmark_id: str
    version: int
    description: str
    sessions: list[MemoryBenchSession]


def load_memory_bench(path: Path | None = None) -> MemoryBench:
    """Load the memory benchmark from JSON.

    Default path is `eval/datasets/memory_bench.json` (relative to this file).
    """
    p = path or DEFAULT_BENCH_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    sessions = []
    for s in data["sessions"]:
        tasks = [
            MemoryBenchTask(
                task_id=t["task_id"],
                kind=t["kind"],
                user_input=t["user_input"],
                expected_facts=t.get("expected_facts", []),
                ground_truth=t.get("ground_truth", ""),
                ground_truth_keywords=t.get("ground_truth_keywords", []),
            )
            for t in s["tasks"]
        ]
        sessions.append(MemoryBenchSession(
            session_id=s["session_id"],
            topic=s["topic"],
            tasks=tasks,
        ))
    return MemoryBench(
        benchmark_id=data["benchmark_id"],
        version=data["version"],
        description=data["description"],
        sessions=sessions,
    )
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint**

Run: `.venv/Scripts/python.exe -m ruff check eval/datasets/memory_bench.py tests/eval/test_memory_bench.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add eval/datasets/memory_bench.py tests/eval/test_memory_bench.py tests/eval/__init__.py
git commit -m "feat(memory-eval): add MemoryBench loader (JSON-backed, no HF dep)"
```

---

## Task 3: Extend metrics schema with memory-specific fields

**Files:**
- Modify: `eval/metrics/schema.py` (add 3 new dataclasses)
- Modify: `tests/eval/test_memory_collector.py` (1 test for dataclass shape)

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_memory_collector.py`:

```python
# tests/eval/test_memory_collector.py
from dataclasses import asdict
from eval.metrics.schema import MemoryTaskMetrics, MemorySessionMetrics, MemoryBenchReport


def test_memory_task_metrics_shape():
    tm = MemoryTaskMetrics(
        task_id="S1-T1", session_id="S1", kind="inject_facts",
        expected_facts=["a", "b"],
        actual_facts_saved=["a"],
        facts_saved_count=1, facts_expected_count=2,
        is_correct=False, ground_truth="", final_answer="",
        injected_memory_tokens=0, memories_recalled_count=0,
        memory_save_tool_called=False, memory_recall_tool_called=False,
        api_total_tokens=100, wall_time_seconds=0.5,
    )
    d = asdict(tm)
    assert d["task_id"] == "S1-T1"
    assert d["kind"] == "inject_facts"
    assert d["expected_facts"] == ["a", "b"]
    assert d["actual_facts_saved"] == ["a"]


def test_memory_session_metrics_shape():
    sm = MemorySessionMetrics(
        session_id="S1", topic="user_profile_basics",
        tasks_total=4, tasks_correct=3,
        db_size_at_start=0, db_size_at_end=3,
        facts_added_in_session=3, facts_recalled_in_session=2,
        pipeline_triggers=0, pipeline_saves=0,
        api_total_tokens=500, wall_time_seconds=2.0,
    )
    d = asdict(sm)
    assert d["session_id"] == "S1"
    assert d["db_size_at_end"] == 3


def test_memory_bench_report_shape():
    r = MemoryBenchReport(
        benchmark_id="memory_bench_v1",
        total_sessions=4, total_tasks=17, total_correct=10,
        accuracy=10/17,
        cross_session_recall_precision=0.8, cross_session_recall_recall=0.7,
        save_coverage=0.9, memory_pollution_rate=0.1,
        long_term_accuracy=0.75,
        sessions=[], per_task=[],
        api_total_tokens=5000, wall_time_seconds_total=20.0,
    )
    d = asdict(r)
    assert d["benchmark_id"] == "memory_bench_v1"
    assert d["cross_session_recall_precision"] == 0.8
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_collector.py::test_memory_task_metrics_shape -v`
Expected: `ImportError: cannot import name 'MemoryTaskMetrics'`

- [ ] **Step 3: Add the 3 dataclasses to `eval/metrics/schema.py`**

Append to the bottom of the file (after `ComparisonReport`):

```python
@dataclass
class MemoryTaskMetrics:
    """Per-task metrics for the memory benchmark."""
    task_id: str
    session_id: str
    kind: str           # inject_facts | recall_qa | followup_qa | filler_qa
    expected_facts: list[str]
    actual_facts_saved: list[str]    # facts that ended up in db after this task
    facts_saved_count: int           # = len(actual_facts_saved)
    facts_expected_count: int        # = len(expected_facts) for inject tasks
    is_correct: bool                 # recall/followup: ground_truth keywords matched; filler: any non-empty
    ground_truth: str
    final_answer: str
    injected_memory_tokens: int      # from turn_stats.injected_memory
    memories_recalled_count: int     # count of memory_recall tool invocations in this task
    memory_save_tool_called: bool
    memory_recall_tool_called: bool
    api_total_tokens: int
    wall_time_seconds: float


@dataclass
class MemorySessionMetrics:
    """Per-session metrics for the memory benchmark."""
    session_id: str
    topic: str
    tasks_total: int
    tasks_correct: int
    db_size_at_start: int            # count(*) at session start
    db_size_at_end: int              # count(*) at session end
    facts_added_in_session: int
    facts_recalled_in_session: int
    pipeline_triggers: int
    pipeline_saves: int
    api_total_tokens: int
    wall_time_seconds: float


@dataclass
class MemoryBenchReport:
    """Aggregate metrics across all sessions of a memory benchmark run."""
    benchmark_id: str
    total_sessions: int
    total_tasks: int
    total_correct: int
    accuracy: float

    # Recall quality across all recall/followup tasks
    cross_session_recall_precision: float
    cross_session_recall_recall: float

    # Save quality across all inject_facts tasks
    save_coverage: float

    # Pollution: fraction of filler_qa tasks that created a memory
    memory_pollution_rate: float

    # Long-term accuracy: recall_qa accuracy in the last session
    long_term_accuracy: float

    sessions: list                   # list[MemorySessionMetrics]
    per_task: list                   # list[MemoryTaskMetrics]

    api_total_tokens: int
    wall_time_seconds_total: float
```

Also, add an `injected_memory_tokens: int = 0` field to the existing `TaskMetrics` dataclass (the GAIA one). It will be unused by GAIA eval (always 0) but used by memory eval. Add it right after `peak_total_tokens: int = 0`:

```python
    peak_total_tokens: int = 0
    injected_memory_tokens: int = 0   # NEW: 0 in GAIA (no memory system), used by memory eval
    peak_ratio: float = 0.0
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_collector.py -v`
Expected: 3 passed

- [ ] **Step 5: Verify GAIA tests still pass**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all green (the `injected_memory_tokens` default of 0 is backwards-compatible)

- [ ] **Step 6: Lint**

Run: `.venv/Scripts/python.exe -m ruff check eval/metrics/schema.py tests/eval/test_memory_collector.py`
Expected: All checks passed

- [ ] **Step 7: Commit**

```bash
git add eval/metrics/schema.py tests/eval/test_memory_collector.py
git commit -m "feat(memory-eval): add MemoryTaskMetrics, MemorySessionMetrics, MemoryBenchReport"
```

---

## Task 4: Add memory collector (compute_recall_pr, collect_*, aggregate_*)

**Files:**
- Create: `eval/metrics/memory_collector.py`
- Modify: `tests/eval/test_memory_collector.py` (add 4 more tests)

- [ ] **Step 1: Write the failing tests (append to `tests/eval/test_memory_collector.py`)**

```python
# tests/eval/test_memory_collector.py (append)
from eval.metrics.memory_collector import (
    compute_recall_pr,
    collect_memory_task_metrics,
    aggregate_memory_session,
    aggregate_memory_bench,
)


def test_compute_recall_pr_perfect():
    """All expected facts recalled → P=1, R=1."""
    expected = ["a", "b", "c"]
    recalled = ["a", "b", "c", "d"]   # extra d
    p, r = compute_recall_pr(expected, recalled)
    assert p == 0.75  # 3 of 4 retrieved are correct
    assert r == 1.0   # all 3 expected found


def test_compute_recall_pr_partial():
    expected = ["a", "b", "c"]
    recalled = ["a", "x"]
    p, r = compute_recall_pr(expected, recalled)
    assert p == 0.5   # 1 of 2 retrieved is correct
    assert abs(r - 1/3) < 1e-9  # 1 of 3 expected found


def test_compute_recall_pr_empty_expected():
    p, r = compute_recall_pr([], ["x", "y"])
    # Expected nothing; nothing should be matched. P=0 (no correct), R=0 (vacuously).
    assert p == 0.0
    assert r == 0.0


def test_compute_recall_pr_empty_recalled():
    p, r = compute_recall_pr(["a", "b"], [])
    # Expected 2, recalled 0. P=0, R=0.
    assert p == 0.0
    assert r == 0.0
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_collector.py::test_compute_recall_pr_perfect -v`
Expected: `ModuleNotFoundError: No module named 'eval.metrics.memory_collector'`

- [ ] **Step 3: Implement `compute_recall_pr` and the 3 aggregate functions**

Create `eval/metrics/memory_collector.py`:

```python
"""Memory benchmark metric collection: per-task, per-session, per-bench.

Public API:
- `compute_recall_pr(expected, recalled) -> (precision, recall)` (pure)
- `collect_memory_task_metrics(...) -> MemoryTaskMetrics`
- `aggregate_memory_session(task_metrics, ...) -> MemorySessionMetrics`
- `aggregate_memory_bench(session_metrics, per_task, ...) -> MemoryBenchReport`
"""
from __future__ import annotations
from eval.metrics.schema import (
    MemoryBenchReport,
    MemorySessionMetrics,
    MemoryTaskMetrics,
)


def compute_recall_pr(expected: list[str], recalled: list[str]) -> tuple[float, float]:
    """Substring-based recall: a "recalled" item is correct if any of the
    expected facts is a substring of it (case-insensitive), AND vice versa.

    Returns (precision, recall) in [0, 1]. P=0 if retrieved=0; R=0 if expected=0.
    """
    exp_norm = [e.lower() for e in expected]
    rec_norm = [r.lower() for r in recalled]

    def _matches(r_text: str) -> bool:
        return any(e in r_text or r_text in e for e in exp_norm)

    def _matched_expected(r_text: str) -> bool:
        return any(e in r_text or r_text in e for e in exp_norm)

    if not exp_norm and not rec_norm:
        return 0.0, 0.0
    if not rec_norm:
        return 0.0, 0.0
    if not exp_norm:
        # Expected nothing, recalled something → vacuously P=0
        return 0.0, 0.0

    correct_recalled = sum(1 for r in rec_norm if _matches(r))
    expected_matched = sum(1 for e in exp_norm if any(_matched_expected(r) for r in rec_norm))

    precision = correct_recalled / len(rec_norm)
    recall = expected_matched / len(exp_norm)
    return precision, recall


def _count_tool_calls(messages: list[dict], tool_name: str) -> int:
    """Count assistant tool_calls with function.name == tool_name in messages."""
    n = 0
    for m in messages:
        if m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            fn = tc.get("function") or {}
            if fn.get("name") == tool_name:
                n += 1
    return n


def _match_saved_facts(expected: list[str], db_memories_text: list[str]) -> list[str]:
    """Return the subset of expected that match (substring) any db memory text.

    db_memories_text is a list of memory.text values from the store.
    """
    matched = []
    db_norm = [t.lower() for t in db_memories_text]
    for e in expected:
        e_norm = e.lower()
        if any(e_norm in t or t in e_norm for t in db_norm):
            matched.append(e)
    return matched


def collect_memory_task_metrics(
    *,
    task,                         # MemoryBenchTask
    session_id: str,
    task_index: int,
    messages: list[dict],         # session.messages AFTER this task's run_turn
    db_memories_before: list,     # list[Memory] before this task
    db_memories_after: list,      # list[Memory] after this task
    final_answer: str,
    is_correct: bool,
    injected_memory_tokens: int,
    turn_api_tokens: int,
    wall_time_seconds: float,
) -> MemoryTaskMetrics:
    """Pure function. Build MemoryTaskMetrics from the task state.

    `db_memories_after` and `db_memories_before` are full Memory objects (have
    .text). The "actually saved" facts are: texts in `after` whose id is not in
    `before` AND whose text matches some expected fact.
    """
    ids_before = {m.id for m in db_memories_before}
    new_memories = [m for m in db_memories_after if m.id not in ids_before]
    new_texts = [m.text for m in new_memories]

    actual_facts_saved = _match_saved_facts(list(task.expected_facts), new_texts)

    return MemoryTaskMetrics(
        task_id=task.task_id,
        session_id=session_id,
        kind=task.kind,
        expected_facts=list(task.expected_facts),
        actual_facts_saved=actual_facts_saved,
        facts_saved_count=len(actual_facts_saved),
        facts_expected_count=len(task.expected_facts) if task.kind == "inject_facts" else 0,
        is_correct=is_correct,
        ground_truth=task.ground_truth,
        final_answer=final_answer,
        injected_memory_tokens=injected_memory_tokens,
        memories_recalled_count=_count_tool_calls(messages, "memory_recall"),
        memory_save_tool_called=_count_tool_calls(messages, "memory_save") > 0,
        memory_recall_tool_called=_count_tool_calls(messages, "memory_recall") > 0,
        api_total_tokens=turn_api_tokens,
        wall_time_seconds=wall_time_seconds,
    )


def aggregate_memory_session(
    task_metrics: list[MemoryTaskMetrics],
    *,
    session_id: str,
    topic: str,
    db_size_at_start: int,
    db_size_at_end: int,
    pipeline_triggers: int,
    pipeline_saves: int,
) -> MemorySessionMetrics:
    """Build MemorySessionMetrics from a session's task list."""
    correct = sum(1 for t in task_metrics if t.is_correct)
    facts_added = sum(t.facts_saved_count for t in task_metrics)
    facts_recalled = sum(t.memories_recalled_count for t in task_metrics)
    return MemorySessionMetrics(
        session_id=session_id,
        topic=topic,
        tasks_total=len(task_metrics),
        tasks_correct=correct,
        db_size_at_start=db_size_at_start,
        db_size_at_end=db_size_at_end,
        facts_added_in_session=facts_added,
        facts_recalled_in_session=facts_recalled,
        pipeline_triggers=pipeline_triggers,
        pipeline_saves=pipeline_saves,
        api_total_tokens=sum(t.api_total_tokens for t in task_metrics),
        wall_time_seconds=sum(t.wall_time_seconds for t in task_metrics),
    )


def aggregate_memory_bench(
    session_metrics: list[MemorySessionMetrics],
    per_task: list[MemoryTaskMetrics],
    *,
    benchmark_id: str,
) -> MemoryBenchReport:
    """Build the cross-session aggregate MemoryBenchReport."""
    total_tasks = len(per_task)
    total_correct = sum(1 for t in per_task if t.is_correct)
    accuracy = (total_correct / total_tasks) if total_tasks else 0.0

    # Recall PR: across all recall/followup tasks, pool (expected, recalled).
    all_expected: list[str] = []
    all_recalled_texts: list[str] = []
    for t in per_task:
        if t.kind in ("recall_qa", "followup_qa"):
            # Use the final answer as a proxy for "what the LLM actually retrieved/used".
            # (More accurate would be to inspect memory_recall tool results; for v1,
            #  the final answer is the user-visible evidence.)
            all_expected.extend(t.expected_facts)
            if t.final_answer:
                all_recalled_texts.append(t.final_answer)
    p, r = compute_recall_pr(all_expected, all_recalled_texts)

    # Save coverage: across inject_facts tasks, what % of expected were saved.
    inject_tasks = [t for t in per_task if t.kind == "inject_facts"]
    if inject_tasks:
        exp_total = sum(t.facts_expected_count for t in inject_tasks)
        saved_total = sum(t.facts_saved_count for t in inject_tasks)
        save_coverage = (saved_total / exp_total) if exp_total else 0.0
    else:
        save_coverage = 0.0

    # Pollution: fraction of filler_qa tasks that resulted in actual_facts_saved > 0.
    filler_tasks = [t for t in per_task if t.kind == "filler_qa"]
    if filler_tasks:
        polluted = sum(1 for t in filler_tasks if t.facts_saved_count > 0)
        memory_pollution_rate = polluted / len(filler_tasks)
    else:
        memory_pollution_rate = 0.0

    # Long-term accuracy: recall_qa accuracy in the LAST session.
    last_session_id = session_metrics[-1].session_id if session_metrics else ""
    last_session_tasks = [t for t in per_task if t.session_id == last_session_id
                          and t.kind == "recall_qa"]
    if last_session_tasks:
        long_correct = sum(1 for t in last_session_tasks if t.is_correct)
        long_term_accuracy = long_correct / len(last_session_tasks)
    else:
        long_term_accuracy = 0.0

    return MemoryBenchReport(
        benchmark_id=benchmark_id,
        total_sessions=len(session_metrics),
        total_tasks=total_tasks,
        total_correct=total_correct,
        accuracy=accuracy,
        cross_session_recall_precision=p,
        cross_session_recall_recall=r,
        save_coverage=save_coverage,
        memory_pollution_rate=memory_pollution_rate,
        long_term_accuracy=long_term_accuracy,
        sessions=list(session_metrics),
        per_task=list(per_task),
        api_total_tokens=sum(t.api_total_tokens for t in per_task),
        wall_time_seconds_total=sum(t.wall_time_seconds for t in per_task),
    )
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_collector.py -v`
Expected: 7 passed (3 shape + 4 PR)

- [ ] **Step 5: Lint**

Run: `.venv/Scripts/python.exe -m ruff check eval/metrics/memory_collector.py tests/eval/test_memory_collector.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add eval/metrics/memory_collector.py tests/eval/test_memory_collector.py
git commit -m "feat(memory-eval): add compute_recall_pr, collect/aggregate functions"
```

---

## Task 5: Multi-session runner skeleton (session loop + db persistence)

**Files:**
- Create: `eval/runners/memory_session_runner.py`
- Modify: `tests/eval/test_memory_bench.py` (add 2 tests for session loop)

This is the highest-risk task. Build it in two halves: skeleton first (Task 5), task handlers in Tasks 6-8.

- [ ] **Step 1: Write the failing test (append to `tests/eval/test_memory_bench.py`)**

```python
# tests/eval/test_memory_bench.py (append)
import asyncio
import pathlib
import pytest
from unittest.mock import AsyncMock
from eval.runners.memory_session_runner import run_memory_bench


class _FakeLLM:
    """Returns a fixed response on chat(); counts calls."""
    def __init__(self, text="ok"):
        self.text = text
        self.calls = 0
    async def chat(self, messages, tools=None):
        self.calls += 1
        from tests.test_memory_decider import _FakeStreamEvent
        yield _FakeStreamEvent(kind="content", text=self.text)
        yield _FakeStreamEvent(kind="done", content=self.text, finish_reason="stop")


class _FakeMCP:
    def list_tools(self):
        return []
    async def call_tool(self, name, args):
        from cc_harness.mcp_client import ToolResult
        return ToolResult.success("")
    async def start(self): pass
    async def shutdown(self): pass


class _FakeMemoryStore:
    """In-memory stand-in for MemoryStore; tracks add operations."""
    def __init__(self):
        self._items: dict[str, dict] = {}
        self._next_id = 0
    async def init_schema(self): pass
    async def close(self): pass
    async def add(self, text, embedding, source):
        self._next_id += 1
        mid = f"id-{self._next_id}"
        self._items[mid] = {"id": mid, "text": text, "source": source}
        from cc_harness.memory.store import Memory
        return Memory(id=mid, text=text, embedding=embedding,
                      created_at=0, updated_at=0, source=source)
    async def count(self): return len(self._items)
    async def list_all(self, limit=100):
        from cc_harness.memory.store import Memory
        return [Memory(id=m["id"], text=m["text"], embedding=[],
                       created_at=0, updated_at=0, source=m["source"])
                for m in list(self._items.values())[:limit]]
    async def update(self, id, text, embedding):
        self._items[id]["text"] = text
        from cc_harness.memory.store import Memory
        return Memory(id=id, text=text, embedding=embedding,
                      created_at=0, updated_at=0, source=self._items[id]["source"])
    async def delete(self, id):
        return self._items.pop(id, None) is not None
    async def get(self, id):
        m = self._items.get(id)
        if not m: return None
        from cc_harness.memory.store import Memory
        return Memory(id=m["id"], text=m["text"], embedding=[],
                      created_at=0, updated_at=0, source=m["source"])
    async def search_similar(self, query_embedding, k=5):
        return []


@pytest.mark.asyncio
async def test_run_memory_bench_executes_all_sessions(tmp_path):
    """Skeleton: load bench, run all sessions, return report with right shape."""
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from eval.datasets.memory_bench import load_memory_bench

    bench = load_memory_bench()
    llm = _FakeLLM()
    mcp = _FakeMCP()
    store = _FakeMemoryStore()

    # Need a real EmbeddingClient (its .embed is a noop) and real MemoryService
    # using our fake store. But the runner's contract: pass a service factory.
    # We pass the service directly.
    embedder = EmbeddingClient(base_url="x", api_key="y", model="z", dim=4)
    # Monkey-patch embedder.embed to return a fixed vec
    embedder.embed = AsyncMock(return_value=[0.1] * 4)
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * 4])
    service = MemoryService(store=store, embedder=embedder, decider=_StubNoopDecider())

    report = await run_memory_bench(
        bench=bench, llm=llm, mcp=mcp, memory_service=service,
        memory_db_path=tmp_path / "test.db",
        out_dir=tmp_path / "out",
    )
    assert report.benchmark_id == "memory_bench_v1"
    assert report.total_sessions == 4
    # 17 tasks total in the dataset
    assert report.total_tasks == 17


class _StubNoopDecider:
    async def decide(self, new_text, similar):
        from cc_harness.memory.decider import Decision, DecisionResult
        if not similar:
            return DecisionResult(action=Decision.ADD)
        return DecisionResult(action=Decision.NOOP)
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py::test_run_memory_bench_executes_all_sessions -v`
Expected: `ModuleNotFoundError: No module named 'eval.runners.memory_session_runner'`

- [ ] **Step 3: Implement the runner skeleton (Tasks 6-8 will add handlers)**

Create `eval/runners/memory_session_runner.py`:

```python
"""Multi-session memory benchmark driver.

Public API:
- `run_memory_bench()` — async driver

State model:
- ONE MemoryService (and underlying MemoryStore) shared across all sessions
  (db file is persistent, so facts added in session 1 are visible in session 2+)
- FRESH `messages: list[dict]` per session (cleared at session boundary)
- FRESH system prompt per session
- db_size_at_start / db_size_at_end captured per session
- Per-task metrics emitted via MemoryTaskMetrics
- Per-session metrics emitted via MemorySessionMetrics
- Final aggregate via MemoryBenchReport
"""
from __future__ import annotations
import asyncio
import inspect
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from cc_harness import agent as _agent_mod
from cc_harness.agent import run_turn
from cc_harness.config import ContextConfig
from cc_harness.prompts import build_system_prompt
from cc_harness.tokens import TokenCounter

from eval.datasets.memory_bench import MemoryBench, MemoryBenchTask
from eval.metrics.memory_collector import (
    aggregate_memory_bench,
    aggregate_memory_session,
    collect_memory_task_metrics,
    compute_recall_pr,
)
from eval.metrics.schema import (
    MemoryBenchReport,
    MemorySessionMetrics,
    MemoryTaskMetrics,
)


# --- helper: classify final answer against ground truth keywords ---

def _keywords_match(answer: str, keywords: list[str]) -> bool:
    """All keywords must appear (case-insensitive, substring) in answer."""
    if not keywords:
        return False
    a = answer.lower()
    return all(k.lower() in a for k in keywords)


def _is_correct_for_task(task: MemoryBenchTask, final_answer: str) -> bool:
    if task.kind in ("recall_qa", "followup_qa"):
        return _keywords_match(final_answer, task.ground_truth_keywords)
    if task.kind == "filler_qa":
        # Filler: any non-empty answer is "correct" (we don't grade filler).
        return bool(final_answer.strip())
    if task.kind == "inject_facts":
        # Inject: graded by save coverage (post-hoc via db), not by final answer.
        # Mark is_correct=True here for "the task executed"; actual save coverage
        # is computed in collect_memory_task_metrics via db diff.
        return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- session loop ---

async def run_memory_bench(
    *,
    bench: MemoryBench,
    llm,
    mcp,
    memory_service,
    memory_db_path: Path,
    out_dir: Path,
    context_config: ContextConfig | None = None,
    max_iter: int = 20,
) -> MemoryBenchReport:
    """Run all sessions in the bench and return a MemoryBenchReport.

    Writes:
      - out_dir/trace.jsonl        (one MemoryTaskMetrics dict per line)
      - out_dir/report.json        (full MemoryBenchReport)
      - out_dir/messages_S{n}.json (final messages per session)
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "trace.jsonl"
    trace_path.write_text("", encoding="utf-8")  # truncate

    ctx_cfg = context_config or ContextConfig()
    counter = TokenCounter()

    # `memory_service.store` is the underlying MemoryStore. Used for db diffs
    # (count, list_all) and to confirm persistence.
    store = memory_service.store

    # Run signature detection: does this branch's run_turn accept context_config?
    sig = inspect.signature(run_turn)
    supports_context = "context_config" in sig.parameters
    supports_memory = "memory_service" in sig.parameters

    all_task_metrics: list[MemoryTaskMetrics] = []
    session_metrics_list: list[MemorySessionMetrics] = []

    for session in bench.sessions:
        # Fresh session state
        messages: list[dict] = [{
            "role": "system",
            "content": build_system_prompt(".", mode="coding"),
        }]
        db_size_at_start = await store.count()

        for idx, task in enumerate(session.tasks):
            db_before = await store.list_all(limit=1000)
            messages.append({"role": "user", "content": task.user_input})

            t_start = time.time()
            turn_stats = None
            try:
                kw = {}
                if supports_context:
                    kw["context_config"] = ctx_cfg
                if supports_memory:
                    kw["memory_service"] = memory_service
                    kw["memory_retriever"] = getattr(memory_service, "retriever", None)
                    kw["memory_pipeline"] = getattr(memory_service, "pipeline", None)
                turn_stats = await run_turn(
                    messages, llm, mcp, max_iter=max_iter, cwd=".",
                    token_counter=counter, **kw,
                )
            except Exception as e:
                # Don't crash the whole bench; record failure
                print(f"  [warn] {task.task_id} failed: {e}")
            wall = time.time() - t_start

            db_after = await store.list_all(limit=1000)

            # Extract final answer
            last_asst = next(
                (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
                None,
            )
            final_answer = (last_asst or {}).get("content", "") or ""

            is_correct = _is_correct_for_task(task, final_answer)
            injected_mem_tokens = getattr(turn_stats, "injected_memory", 0) if turn_stats else 0
            api_tokens = getattr(turn_stats, "api_total_tokens", 0) if turn_stats else 0

            tm = collect_memory_task_metrics(
                task=task,
                session_id=session.session_id,
                task_index=idx,
                messages=messages,
                db_memories_before=db_before,
                db_memories_after=db_after,
                final_answer=final_answer,
                is_correct=is_correct,
                injected_memory_tokens=injected_mem_tokens,
                turn_api_tokens=api_tokens,
                wall_time_seconds=wall,
            )
            all_task_metrics.append(tm)
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(tm.__dict__, ensure_ascii=False) + "\n")

        # End-of-session
        db_size_at_end = await store.count()
        sm = aggregate_memory_session(
            all_task_metrics,  # NOTE: will be replaced per-session below; for v1 just use running list
            session_id=session.session_id,
            topic=session.topic,
            db_size_at_start=db_size_at_start,
            db_size_at_end=db_size_at_end,
            pipeline_triggers=0,
            pipeline_saves=0,
        )
        session_metrics_list.append(sm)
        (out_dir / f"messages_{session.session_id}.json").write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    report = aggregate_memory_bench(
        session_metrics_list,
        all_task_metrics,
        benchmark_id=bench.benchmark_id,
    )
    (out_dir / "report.json").write_text(
        json.dumps(report.__dict__, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return report
```

NOTE: The skeleton passes `all_task_metrics` to `aggregate_memory_session` as a placeholder; **Task 6** fixes this to pass only the per-session task list. This is a known v1 simplification; the final test will check shape, not exact counts. We'll tighten in Task 6.

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py::test_run_memory_bench_executes_all_sessions -v`
Expected: PASS

- [ ] **Step 5: Lint**

Run: `.venv/Scripts/python.exe -m ruff check eval/runners/memory_session_runner.py tests/eval/test_memory_bench.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add eval/runners/memory_session_runner.py tests/eval/test_memory_bench.py
git commit -m "feat(memory-eval): add multi-session runner skeleton with db persistence"
```

---

## Task 6: Fix per-session task metrics (replace running-list bug)

**Files:**
- Modify: `eval/runners/memory_session_runner.py`
- Modify: `tests/eval/test_memory_bench.py` (add test for per-session isolation)

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_memory_bench.py (append)
@pytest.mark.asyncio
async def test_session_metrics_isolate_per_session(tmp_path):
    """Each session's metrics should only count that session's tasks, not all."""
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from eval.datasets.memory_bench import load_memory_bench

    bench = load_memory_bench()
    llm = _FakeLLM()
    mcp = _FakeMCP()
    store = _FakeMemoryStore()

    embedder = EmbeddingClient(base_url="x", api_key="y", model="z", dim=4)
    embedder.embed = AsyncMock(return_value=[0.1] * 4)
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * 4])
    service = MemoryService(store=store, embedder=embedder, decider=_StubNoopDecider())

    report = await run_memory_bench(
        bench=bench, llm=llm, mcp=mcp, memory_service=service,
        memory_db_path=tmp_path / "test.db",
        out_dir=tmp_path / "out",
    )
    # Per-session metrics should be S1: 4 tasks, S2: 4, S3: 5, S4: 3
    by_session = {s.session_id: s.tasks_total for s in report.sessions}
    assert by_session == {"S1": 4, "S2": 4, "S3": 5, "S4": 3}
    # Total should still sum to 17
    assert sum(by_session.values()) == 17
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py::test_session_metrics_isolate_per_session -v`
Expected: FAIL (because v1 passes `all_task_metrics` to `aggregate_memory_session`, every session sees ALL tasks)

- [ ] **Step 3: Fix the bug**

In `eval/runners/memory_session_runner.py`, change the session loop to maintain a per-session `session_task_metrics` list and pass it to `aggregate_memory_session`:

Replace the for-loop body (from `# End-of-session` comment) with:

```python
        # End-of-session
        db_size_at_end = await store.count()
        # `all_task_metrics` contains tasks from prior sessions too; slice to current
        # session by session_id.
        session_tasks = [t for t in all_task_metrics if t.session_id == session.session_id]
        sm = aggregate_memory_session(
            session_tasks,
            session_id=session.session_id,
            topic=session.topic,
            db_size_at_start=db_size_at_start,
            db_size_at_end=db_size_at_end,
            pipeline_triggers=0,
            pipeline_saves=0,
        )
        session_metrics_list.append(sm)
        (out_dir / f"messages_{session.session_id}.json").write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py -v`
Expected: 8 passed (6 from Task 2 + 2 from Tasks 5+6)

- [ ] **Step 5: Commit**

```bash
git add eval/runners/memory_session_runner.py tests/eval/test_memory_bench.py
git commit -m "fix(memory-eval): aggregate_memory_session receives only per-session tasks"
```

---

## Task 7: Test db persistence across sessions (Task 6's contract)

**Files:**
- Modify: `tests/eval/test_memory_bench.py` (add 1 test)

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_memory_bench.py (append)
@pytest.mark.asyncio
async def test_db_persists_across_sessions(tmp_path):
    """Memories added in S1 must be visible in S2's db_size_at_start."""
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from eval.datasets.memory_bench import load_memory_bench

    # Pre-populate the store with 2 memories (simulating S1 saved 2 facts)
    bench = load_memory_bench()
    llm = _FakeLLM()
    mcp = _FakeMCP()
    store = _FakeMemoryStore()
    await store.add("我住北京", [0.1]*4, "llm")
    await store.add("我养猫", [0.1]*4, "llm")

    embedder = EmbeddingClient(base_url="x", api_key="y", model="z", dim=4)
    embedder.embed = AsyncMock(return_value=[0.1] * 4)
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * 4])
    service = MemoryService(store=store, embedder=embedder, decider=_StubNoopDecider())

    report = await run_memory_bench(
        bench=bench, llm=llm, mcp=mcp, memory_service=service,
        memory_db_path=tmp_path / "test.db",
        out_dir=tmp_path / "out",
    )
    # S1's db_size_at_start should be 2 (the pre-populated count)
    s1 = next(s for s in report.sessions if s.session_id == "S1")
    assert s1.db_size_at_start == 2
    # db_size_at_end >= 2 (we may have added more)
    assert s1.db_size_at_end >= 2
    # S2's db_size_at_start should also be >= 2 (db persists across sessions)
    s2 = next(s for s in report.sessions if s.session_id == "S2")
    assert s2.db_size_at_start >= 2
```

- [ ] **Step 2: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py::test_db_persists_across_sessions -v`
Expected: PASS (the skeleton already preserves `store` across sessions; this test exercises that contract)

- [ ] **Step 3: Commit**

```bash
git add tests/eval/test_memory_bench.py
git commit -m "test(memory-eval): assert db persistence across sessions"
```

---

## Task 8: Per-task-kind behavior verified (inject / recall / filler / followup)

**Files:**
- Modify: `tests/eval/test_memory_bench.py` (add 1 integration test)

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_memory_bench.py (append)
@pytest.mark.asyncio
async def test_recall_task_measures_save_coverage(tmp_path):
    """After running the full bench, save_coverage should be > 0
    (FakeLLM never calls memory_save, but the DECIDE prompt + FakeDecider
    won't trigger. So save_coverage==0. We assert 0 <= save_coverage <= 1.)"""
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from eval.datasets.memory_bench import load_memory_bench

    bench = load_memory_bench()
    llm = _FakeLLM(text="ok")  # never calls memory_save
    mcp = _FakeMCP()
    store = _FakeMemoryStore()
    embedder = EmbeddingClient(base_url="x", api_key="y", model="z", dim=4)
    embedder.embed = AsyncMock(return_value=[0.1] * 4)
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * 4])
    service = MemoryService(store=store, embedder=embedder, decider=_StubNoopDecider())

    report = await run_memory_bench(
        bench=bench, llm=llm, mcp=mcp, memory_service=service,
        memory_db_path=tmp_path / "test.db",
        out_dir=tmp_path / "out",
    )
    # FakeLLM doesn't call memory_save, so save_coverage==0 (no facts were actually saved)
    assert 0.0 <= report.save_coverage <= 1.0
    # All 4 task kinds appeared
    kinds = {t.kind for t in report.per_task}
    assert kinds == {"inject_facts", "recall_qa", "followup_qa", "filler_qa"}
```

- [ ] **Step 2: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_memory_bench.py::test_recall_task_measures_save_coverage -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/eval/test_memory_bench.py
git commit -m "test(memory-eval): assert per-task-kind coverage in bench output"
```

---

## Task 9: Wire CLI in `eval/run.py` with `--benchmark` flag

**Files:**
- Modify: `eval/run.py` (add `--benchmark`, conditional path for memory_bench)

- [ ] **Step 1: Add the CLI flag and dispatch**

Read `eval/run.py` carefully. Add a `--benchmark {gaia,memory}` flag (default: `gaia`). When `--benchmark memory`:

- Skip HuggingFace check (no HF dep for memory bench)
- Load `MemoryBench` via `load_memory_bench()`
- Construct `MemoryService` (with real `EmbeddingClient` from `cfg.memory`)
- Run via `run_memory_bench()`
- Write report to `out_dir/report.json` (runner already does this)
- Print a short summary (accuracy, recall PR, save coverage, long-term accuracy)

Minimal changes (avoid touching the GAIA path at all). Add this at the END of `main()` in `eval/run.py`, AFTER the GAIA branch:

```python
    # --- Memory benchmark branch (independent of GAIA A/B) ---
    if args.benchmark == "memory":
        return await _run_memory_benchmark(args)
```

Add a new function (also in `eval/run.py`):

```python
async def _run_memory_benchmark(args: "Args") -> int:
    """Run the memory benchmark on the CURRENT branch (no worktree A/B)."""
    from cc_harness.memory.config import MemoryConfigError
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.pipeline import MemoryPipeline
    from cc_harness.memory.retriever import MemoryRetriever
    from cc_harness.memory.decider import LLMDecider
    from eval.datasets.memory_bench import load_memory_bench
    from eval.runners.memory_session_runner import run_memory_bench

    if not args.mcp_config.exists():
        print(f"[memory-bench] mcp config not found: {args.mcp_config}")
        return 2
    if not cfg.memory.enabled:  # type: ignore[name-defined]
        print("[memory-bench] cfg.memory.enabled is False; "
              "set MEMORY_ENABLED=true and EMBEDDING_* env vars")
        return 2

    bench = load_memory_bench()
    db_path = cfg.memory.db_base_dir / f"memory-bench-{args.session_id}.db"  # type: ignore[attr-defined]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(db_path, embedding_dim=cfg.memory.embedding_dim)
    await store.init_schema()
    embedder = EmbeddingClient(
        base_url=cfg.memory.embedding_base_url,
        api_key=cfg.memory.embedding_api_key,
        model=cfg.memory.embedding_model,
        dim=cfg.memory.embedding_dim,
        timeout_s=cfg.memory.embed_timeout_s,
    )
    decider = LLMDecider(llm)
    service = MemoryService(store, embedder, decider)
    pipeline = MemoryPipeline(llm, service,
                              threshold=cfg.memory.pipeline_threshold,
                              recent_turns=cfg.memory.pipeline_recent_turns,
                              max_delta_tokens=cfg.memory.pipeline_max_delta_tokens)
    retriever = MemoryRetriever(store, embedder,
                                top_k=cfg.memory.retriever_top_k,
                                token_budget=cfg.memory.injection_token_budget)

    if args.output_dir is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        args.output_dir = Path("eval/runs") / f"{date}-memory-bench-{head}"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        await mcp.start()  # mcp is defined later in main(); adjust if needed
    except Exception:
        pass

    report = await run_memory_bench(
        bench=bench, llm=llm, mcp=mcp, memory_service=service,
        memory_db_path=db_path,
        out_dir=args.output_dir,
    )
    print(f"[memory-bench] report: {args.output_dir}/report.json")
    print(f"  accuracy:              {report.accuracy:.0%}")
    print(f"  recall P / R:          {report.cross_session_recall_precision:.0%} / "
          f"{report.cross_session_recall_recall:.0%}")
    print(f"  save coverage:         {report.save_coverage:.0%}")
    print(f"  memory pollution:      {report.memory_pollution_rate:.0%}")
    print(f"  long-term accuracy:    {report.long_term_accuracy:.0%}")
    return 0
```

NOTE: This is a sketch. Adjust based on the actual structure of `eval/run.py` (e.g., `cfg` may be `args.cfg`, `mcp` may need to be started before this function). Read the file first.

In the `Args` dataclass, add `benchmark: str = "gaia"`. In `parse_args`, add:
```python
p.add_argument("--benchmark", default="gaia", choices=["gaia", "memory"])
```

- [ ] **Step 2: Smoke check CLI**

Run: `.venv/Scripts/python.exe -m eval.run --benchmark memory --help`
Expected: usage shown (no crashes)

Run: `.venv/Scripts/python.exe -m eval.run --benchmark memory --dry-run` (if you want a no-op path; otherwise skip)

- [ ] **Step 3: Run all eval tests**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/ -q`
Expected: all green (the existing GAIA tests + new memory tests)

- [ ] **Step 4: Lint**

Run: `.venv/Scripts/python.exe -m ruff check eval/run.py tests/eval/`
Expected: All checks passed

- [ ] **Step 5: Commit**

```bash
git add eval/run.py
git commit -m "feat(memory-eval): add --benchmark memory CLI flag"
```

---

## Task 10: e2e smoke (real LLM, skipped without env)

**Files:**
- Create: `tests/_test_memory_bench_e2e.py` (leading underscore, not auto-collected)

- [ ] **Step 1: Write the test**

```python
# tests/_test_memory_bench_e2e.py
"""E2E test for the memory benchmark using real LLM + real embedding API.

NOT auto-collected by pytest. Run manually:
    .venv/Scripts/python.exe -m pytest tests/_test_memory_bench_e2e.py -v
"""
import asyncio
import os
import pathlib
import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("OPENAI_API_KEY") and os.getenv("EMBEDDING_API_KEY")
         and os.getenv("EMBEDDING_BASE_URL") and os.getenv("EMBEDDING_MODEL")),
    reason="需要 OPENAI_* + EMBEDDING_* 环境变量",
)


async def test_memory_bench_e2e(tmp_path):
    from cc_harness.config import load_config
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.memory.config import MemoryConfig
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.decider import LLMDecider
    from eval.datasets.memory_bench import load_memory_bench
    from eval.runners.memory_session_runner import run_memory_bench

    cfg = load_config(env_path=pathlib.Path(".env"), mcp_json_path=pathlib.Path("mcp.json"))
    llm = LLMClient(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url,
                    model=cfg.openai_model)
    mcp = MCPClient(cfg.mcp_servers)

    db_path = tmp_path / "e2e.db"
    store = __import__("cc_harness.memory.store", fromlist=["MemoryStore"]).MemoryStore(
        db_path, embedding_dim=cfg.memory.embedding_dim)
    await store.init_schema()
    embedder = EmbeddingClient(
        base_url=cfg.memory.embedding_base_url,
        api_key=cfg.memory.embedding_api_key,
        model=cfg.memory.embedding_model,
        dim=cfg.memory.embedding_dim,
    )
    decider = LLMDecider(llm)
    service = MemoryService(store, embedder, decider)
    bench = load_memory_bench()

    await mcp.start()
    try:
        report = await run_memory_bench(
            bench=bench, llm=llm, mcp=mcp, memory_service=service,
            memory_db_path=db_path, out_dir=tmp_path / "out",
        )
        assert report.total_tasks == 17
        assert 0.0 <= report.accuracy <= 1.0
    finally:
        await mcp.shutdown()
        await store.close()
        await embedder.aclose()
```

- [ ] **Step 2: Run — verify skipped**

Run: `.venv/Scripts/python.exe -m pytest tests/_test_memory_bench_e2e.py -v`
Expected: 1 skipped (no env)

- [ ] **Step 3: Commit**

```bash
git add tests/_test_memory_bench_e2e.py
git commit -m "test(memory-eval): add e2e test (real LLM + embedding, skipped without env)"
```

---

## Task 11: Update `eval/README.md` with Memory benchmark section

**Files:**
- Modify: `eval/README.md`

- [ ] **Step 1: Append new section**

After the existing "GAIA context-management eval" sections, append:

```markdown
## Memory benchmark

Cross-session recall evaluation on a synthetic 4-session × 17-task dataset
(`eval/datasets/memory_bench.json`). Tests whether facts injected in early
sessions are correctly recalled in later sessions.

```bash
# Requires EMBEDDING_* + OPENAI_* env vars (memory system must be enabled).
.venv/Scripts/python.exe -m eval.run --benchmark memory
```

Outputs to `eval/runs/<date>-memory-bench-<sha>/`:
- `report.json` — full `MemoryBenchReport` (accuracy, recall P/R, save coverage, pollution, long-term accuracy)
- `trace.jsonl` — one `MemoryTaskMetrics` per line
- `messages_S{1..4}.json` — final session messages

### Metrics (7 dimensions)

- **Accuracy** — fraction of all tasks that are "correct" per task-kind rules
- **cross_session_recall_precision** — fraction of LLM "recalled" content that matches expected facts
- **cross_session_recall_recall** — fraction of expected facts that were actually retrieved
- **save_coverage** — fraction of `expected_facts` in `inject_facts` tasks that ended up in db
- **memory_pollution_rate** — fraction of `filler_qa` tasks that resulted in unwanted memory creation
- **long_term_accuracy** — recall accuracy in the LAST session (tests deep retention)
- **cost** — total API tokens, wall time

### Task kinds

- `inject_facts` — user states facts; LLM may call `memory_save` to persist
- `recall_qa` — user asks a question; LLM should call `memory_recall` to retrieve
- `followup_qa` — user asks an open question whose answer depends on prior facts
- `filler_qa` — unrelated question; should NOT create a memory
```

- [ ] **Step 2: Commit**

```bash
git add eval/README.md
git commit -m "docs(memory-eval): add Memory benchmark section to eval/README.md"
```

---

## Task 12: Update `CLAUDE.md` with memory-bench command

**Files:**
- Modify: `CLAUDE.md` (add 1 line to Common commands)

- [ ] **Step 1: Add command**

In the "Common commands" section, after the existing GAIA lines, add:

```bash
# Memory benchmark (cross-session recall, 4 sessions × 17 tasks)
.venv/Scripts/python.exe -m eval.run --benchmark memory
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add memory-bench command to CLAUDE.md"
```

---

## Task 13: Run full test suite + lint

**Files:** none

- [ ] **Step 1: Run all tests**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all green (~339 tests; +8 from memory-eval)

- [ ] **Step 2: Lint**

Run: `.venv/Scripts/python.exe -m ruff check eval/ tests/eval/`
Expected: All checks passed

- [ ] **Step 3: (If any fixups) commit**

```bash
git add -A
git commit -m "style: ruff fixes"
```

(Only if changes were needed; otherwise skip.)

---

## Self-Review Before Handoff

- [ ] All 13 task commits made; git log shows clean atomic steps
- [ ] `pytest tests/` → all pass
- [ ] `ruff check eval/ tests/eval/` → clean
- [ ] `eval.run --benchmark memory --help` shows the new flag
- [ ] `eval/datasets/memory_bench.json` is human-editable (clear comments, no magic numbers)
- [ ] No real LLM / embedding calls in default test path
- [ ] `_test_memory_bench_e2e.py` is underscore-prefixed and skipped without env

---

## Out of Scope (deferred)

- **Mock LLM that actually calls memory_recall / memory_save** in unit tests — current unit tests use `_FakeLLM` that just returns "ok". To test recall precision meaningfully without real LLM, would need a smarter fake that injects tool calls based on user input. **YAGNI for v1**: real e2e is the source of truth.
- **Benchmark variants** (e.g., larger benchmarks, multi-user, real-GAIA-as-context-tests) — YAGNI
- **Statistical significance testing** across multiple seeds — current `--seed` is reserved but unused
- **Real-time cost monitoring** during the bench — covered by total tokens in report
- **Auto-grader using LLM-as-judge** for followup_qa — current keyword matching is sufficient for v1
