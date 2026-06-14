# GAIA Context Management Eval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a programmatic evaluation framework in `eval/` that runs GAIA validation tasks through both `master` and `context-compaction` branches under matched conditions, then produces a markdown + CSV + JSON report comparing 11 metrics dimensions (context, accuracy, cost, robustness).

**Architecture:** Pipeline-split package — `datasets/` loads & filters GAIA, `grading/` ports the official scorer, `metrics/` defines schemas + collector, `runners/session_runner.py` drives a single multi-turn session per branch (worktree-isolated), `reports/` renders markdown+csv, `run.py` is the CLI orchestrator. Each branch runs in a `git worktree`, with `cc_harness` imported via temporary `sys.path` prepend so we use that branch's exact code.

**Tech Stack:** Python 3.11+, pytest+pytest-asyncio (existing), `huggingface-hub` + `datasets` (new), `pandas` (CSV emit), pydantic (reuse existing `ContextConfig`), stdlib `argparse` / `dataclasses` / `subprocess` (git worktree).

**Source spec:** `docs/superpowers/specs/2026-06-14-gaia-context-eval-design.md`

---

## Phase 0 — Scaffolding & dependencies

### Task 0.1: Create package skeleton

**Files:**
- Create: `eval/__init__.py` (empty)
- Create: `eval/datasets/__init__.py` (empty)
- Create: `eval/runners/__init__.py` (empty)
- Create: `eval/grading/__init__.py` (empty)
- Create: `eval/metrics/__init__.py` (empty)
- Create: `eval/reports/__init__.py` (empty)
- Create: `tests/eval/__init__.py` (empty)

- [ ] **Step 1: Create all skeleton files**

```bash
mkdir -p eval/datasets eval/runners eval/grading eval/metrics eval/reports tests/eval
touch eval/__init__.py eval/datasets/__init__.py eval/runners/__init__.py eval/grading/__init__.py eval/metrics/__init__.py eval/reports/__init__.py tests/eval/__init__.py
```

- [ ] **Step 2: Verify pytest still collects (no new tests yet, just structure)**

Run: `.venv/Scripts/python.exe -m pytest tests/ --collect-only -q | tail -5`
Expected: still shows ~217 tests, no errors.

- [ ] **Step 3: Commit**

```bash
git add eval/ tests/eval/
git commit -m "feat(eval): scaffold pipeline packages"
```

### Task 0.2: Update .gitignore for eval runs

**Files:**
- Modify: `.gitignore` (append `eval/runs/` and `.eval-worktrees/`)

- [ ] **Step 1: Append to .gitignore**

```
eval/runs/
.eval-worktrees/
~/.cache/gaia-eval/
```

- [ ] **Step 2: Verify**

Run: `grep -E "eval/runs|eval-worktrees" .gitignore`
Expected: both lines printed.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(eval): gitignore runs/ and worktrees"
```

### Task 0.3: Add eval dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml` (add `eval` optional dependency group)

- [ ] **Step 1: Edit pyproject.toml**

Append to `[project.optional-dependencies]`:
```toml
eval = [
  "huggingface-hub>=0.24",
  "datasets>=2.20",
  "pandas>=2.2",
]
```

- [ ] **Step 2: Install the new group**

Run: `.venv/Scripts/python.exe -m pip install -e ".[eval,dev]"`
Expected: installs huggingface-hub, datasets, pandas (and their deps).

- [ ] **Step 3: Smoke-import**

Run: `.venv/Scripts/python.exe -c "import huggingface_hub, datasets, pandas; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(eval): add huggingface-hub, datasets, pandas as eval extras"
```

### Task 0.4: Verify HF auth manually (gated step — human action)

- [ ] **Step 1: Confirm HF_TOKEN is set, or run `huggingface-cli login`** (manual)

Run: `.venv/Scripts/python.exe -c "from huggingface_hub import HfApi; print(HfApi().whoami()['name'])"`
Expected: your HF username printed.

(If it fails: `huggingface-cli login` and paste your token. No commit needed.)

---

## Phase 1 — GAIA dataset loader (`eval/datasets/gaia_loader.py`)

### Task 1.1: Define `GaiaTask` dataclass + suffix constants (test-first)

**Files:**
- Test: `tests/eval/test_gaia_loader.py`
- Create: `eval/datasets/gaia_loader.py`

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_gaia_loader.py
from eval.datasets.gaia_loader import GaiaTask, HARD_GAP_SUFFIXES, SOFT_GAP_SUFFIXES

def test_gaia_task_fields():
    t = GaiaTask(
        task_id="abc-123", question="What is 2+2?", level=1,
        ground_truth="4", file_name=None,
    )
    assert t.task_id == "abc-123"
    assert t.level == 1
    assert t.file_name is None

def test_suffix_constants():
    assert ".png" in HARD_GAP_SUFFIXES
    assert ".mp3" in HARD_GAP_SUFFIXES
    assert ".mp4" in HARD_GAP_SUFFIXES
    assert ".pdf" in SOFT_GAP_SUFFIXES
    assert ".xlsx" in SOFT_GAP_SUFFIXES
    # Disjoint sets
    assert HARD_GAP_SUFFIXES.isdisjoint(SOFT_GAP_SUFFIXES)
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: ImportError on `eval.datasets.gaia_loader`.

- [ ] **Step 3: Implement minimally**

```python
# eval/datasets/gaia_loader.py
"""GAIA validation set loader + tool-capability filter."""
from __future__ import annotations
from dataclasses import dataclass

# File suffixes we have NO way to handle (model is text-only, no MCP coverage).
HARD_GAP_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac",
    ".mp4", ".mov", ".avi", ".webm", ".mkv",
})

# Suffixes we CAN handle via MCP (pdf-reader-mcp / excel-mcp-server / OCR-recognition)
# or via run_command fallback (pandas / pdftotext / unzip).
SOFT_GAP_SUFFIXES: frozenset[str] = frozenset({
    ".pdf", ".xlsx", ".xls", ".csv", ".tsv",
    ".txt", ".json", ".jsonl", ".xml", ".html",
    ".zip", ".tar", ".gz",
})


@dataclass(frozen=True)
class GaiaTask:
    task_id: str
    question: str
    level: int
    ground_truth: str
    file_name: str | None  # None when task has no attachment
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/datasets/gaia_loader.py tests/eval/test_gaia_loader.py
git commit -m "feat(eval): GaiaTask dataclass + suffix gap constants"
```

### Task 1.2: `filter_tasks` function (text/soft/hard partition)

**Files:**
- Modify: `tests/eval/test_gaia_loader.py` (append)
- Modify: `eval/datasets/gaia_loader.py` (append)

- [ ] **Step 1: Add failing tests**

```python
def test_filter_tasks_separates_text_soft_hard():
    from eval.datasets.gaia_loader import filter_tasks
    tasks = [
        GaiaTask("t1", "q1", 1, "a1", None),          # text-only
        GaiaTask("t2", "q2", 1, "a2", "doc.pdf"),     # soft (pdf)
        GaiaTask("t3", "q3", 2, "a3", "data.xlsx"),   # soft (excel)
        GaiaTask("t4", "q4", 1, "a4", "img.png"),     # hard (image)
        GaiaTask("t5", "q5", 1, "a5", "tune.mp3"),    # hard (audio)
        GaiaTask("t6", "q6", 1, "a6", "weird.xyz"),   # unknown -> hard (safe)
    ]
    runnable, skipped = filter_tasks(tasks, include_attachments=True)
    assert {t.task_id for t in runnable} == {"t1", "t2", "t3"}
    assert {t.task_id for t in skipped} == {"t4", "t5", "t6"}

def test_filter_tasks_text_only_when_attachments_disabled():
    from eval.datasets.gaia_loader import filter_tasks
    tasks = [
        GaiaTask("t1", "q1", 1, "a1", None),
        GaiaTask("t2", "q2", 1, "a2", "doc.pdf"),
    ]
    runnable, skipped = filter_tasks(tasks, include_attachments=False)
    assert {t.task_id for t in runnable} == {"t1"}
    assert {t.task_id for t in skipped} == {"t2"}
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: ImportError on `filter_tasks`.

- [ ] **Step 3: Implement**

Append to `eval/datasets/gaia_loader.py`:
```python
def filter_tasks(
    tasks: list[GaiaTask], *, include_attachments: bool = True,
) -> tuple[list[GaiaTask], list[GaiaTask]]:
    """Partition into (runnable, skipped).

    Skipped:
      - any task whose file_name suffix is in HARD_GAP_SUFFIXES
      - any task whose file_name suffix is unknown (treated as hard for safety)
      - if include_attachments is False: any task with a file_name
    """
    runnable, skipped = [], []
    for t in tasks:
        if t.file_name is None:
            runnable.append(t)
            continue
        if not include_attachments:
            skipped.append(t)
            continue
        suffix = "." + t.file_name.rsplit(".", 1)[-1].lower() if "." in t.file_name else ""
        if suffix in SOFT_GAP_SUFFIXES:
            runnable.append(t)
        else:
            skipped.append(t)
    return runnable, skipped
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/datasets/gaia_loader.py tests/eval/test_gaia_loader.py
git commit -m "feat(eval): filter_tasks partition by file-suffix capability"
```

### Task 1.3: `stratified_sample` function (deterministic by seed)

**Files:**
- Modify: `tests/eval/test_gaia_loader.py` (append)
- Modify: `eval/datasets/gaia_loader.py` (append)

- [ ] **Step 1: Add failing tests**

```python
def test_stratified_sample_balances_levels():
    from eval.datasets.gaia_loader import stratified_sample
    tasks = (
        [GaiaTask(f"L1-{i}", "q", 1, "a", None) for i in range(50)]
        + [GaiaTask(f"L2-{i}", "q", 2, "a", None) for i in range(50)]
        + [GaiaTask(f"L3-{i}", "q", 3, "a", None) for i in range(50)]
    )
    out = stratified_sample(tasks, limit=30, seed=42)
    assert len(out) == 30
    counts = {1: 0, 2: 0, 3: 0}
    for t in out:
        counts[t.level] += 1
    # Roughly balanced (10 each, +/- 1 due to rounding)
    assert all(9 <= c <= 11 for c in counts.values())

def test_stratified_sample_deterministic():
    from eval.datasets.gaia_loader import stratified_sample
    tasks = [GaiaTask(f"t-{i}", "q", 1, "a", None) for i in range(100)]
    a = stratified_sample(tasks, limit=10, seed=42)
    b = stratified_sample(tasks, limit=10, seed=42)
    assert [t.task_id for t in a] == [t.task_id for t in b]
    c = stratified_sample(tasks, limit=10, seed=43)
    assert [t.task_id for t in a] != [t.task_id for t in c]

def test_stratified_sample_limit_exceeds_pool():
    from eval.datasets.gaia_loader import stratified_sample
    tasks = [GaiaTask(f"t-{i}", "q", 1, "a", None) for i in range(5)]
    out = stratified_sample(tasks, limit=10, seed=42)
    assert len(out) == 5  # cap at pool size
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `eval/datasets/gaia_loader.py`:
```python
import random as _random


def stratified_sample(
    tasks: list[GaiaTask], *, limit: int, seed: int,
) -> list[GaiaTask]:
    """Sample up to `limit` tasks, balancing across levels.

    If a level has fewer tasks than its share, surplus is redistributed
    to other levels. Deterministic on seed.
    """
    if limit <= 0 or not tasks:
        return []
    rng = _random.Random(seed)
    by_level: dict[int, list[GaiaTask]] = {}
    for t in tasks:
        by_level.setdefault(t.level, []).append(t)
    for lst in by_level.values():
        rng.shuffle(lst)

    levels = sorted(by_level)
    per_level = max(1, limit // len(levels))
    picked: list[GaiaTask] = []
    for lv in levels:
        picked.extend(by_level[lv][:per_level])
    # Fill remaining slots from the largest leftover pools (round-robin)
    remaining = limit - len(picked)
    leftover = {lv: by_level[lv][per_level:] for lv in levels}
    while remaining > 0 and any(leftover.values()):
        for lv in levels:
            if leftover[lv]:
                picked.append(leftover[lv].pop(0))
                remaining -= 1
                if remaining == 0:
                    break
    return picked[:limit]
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/datasets/gaia_loader.py tests/eval/test_gaia_loader.py
git commit -m "feat(eval): stratified_sample with seed-determinism"
```

### Task 1.4: `load_gaia_validation` (HF fetch with cache)

**Files:**
- Modify: `tests/eval/test_gaia_loader.py` (append, with mock)
- Modify: `eval/datasets/gaia_loader.py` (append)

- [ ] **Step 1: Add failing test with mocked HF load**

```python
def test_load_gaia_validation_constructs_tasks(monkeypatch):
    """load_gaia_validation maps HF rows -> GaiaTask correctly."""
    from eval.datasets import gaia_loader

    fake_rows = [
        {"task_id": "id1", "Question": "Q1", "Level": "1",
         "Final answer": "42", "file_name": ""},
        {"task_id": "id2", "Question": "Q2", "Level": "2",
         "Final answer": "yes", "file_name": "data.csv"},
    ]
    class FakeSplit(list):
        pass
    monkeypatch.setattr(
        gaia_loader, "_hf_load_dataset",
        lambda: {"validation": FakeSplit(fake_rows)},
    )

    tasks = gaia_loader.load_gaia_validation()
    assert len(tasks) == 2
    assert tasks[0].task_id == "id1"
    assert tasks[0].level == 1
    assert tasks[0].file_name is None       # empty string -> None
    assert tasks[1].file_name == "data.csv"
    assert tasks[1].level == 2
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py::test_load_gaia_validation_constructs_tasks -v`
Expected: AttributeError on `_hf_load_dataset` or `load_gaia_validation`.

- [ ] **Step 3: Implement**

Append to `eval/datasets/gaia_loader.py`:
```python
def _hf_load_dataset():
    """Indirection so tests can monkeypatch without importing datasets."""
    from datasets import load_dataset  # local import keeps test boot fast
    return load_dataset("gaia-benchmark/GAIA", "2023_all")


def load_gaia_validation() -> list[GaiaTask]:
    """Fetch the GAIA validation split and map rows to GaiaTask.

    Requires HF auth (HF_TOKEN env or `huggingface-cli login`).
    Cached by HF under ~/.cache/huggingface/.
    """
    ds = _hf_load_dataset()
    split = ds["validation"]
    out: list[GaiaTask] = []
    for row in split:
        fname = row.get("file_name") or ""
        out.append(GaiaTask(
            task_id=str(row["task_id"]),
            question=str(row["Question"]),
            level=int(row["Level"]),
            ground_truth=str(row["Final answer"]),
            file_name=fname if fname else None,
        ))
    return out
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_loader.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/datasets/gaia_loader.py tests/eval/test_gaia_loader.py
git commit -m "feat(eval): load_gaia_validation HF fetch + row mapping"
```

---

## Phase 2 — GAIA grader (`eval/grading/gaia_grader.py`)

Port the official GAIA scoring logic (https://huggingface.co/spaces/gaia-benchmark/leaderboard, evaluation script in their repo). The official scorer normalizes both sides, supports numeric tolerance for numbers, and treats comma-separated lists as multisets.

### Task 2.1: `_normalize_str` helper

**Files:**
- Test: `tests/eval/test_gaia_grader.py`
- Create: `eval/grading/gaia_grader.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/eval/test_gaia_grader.py
from eval.grading.gaia_grader import _normalize_str

def test_normalize_strips_articles_punctuation_lower():
    assert _normalize_str("The Eiffel Tower.") == "eiffel tower"
    assert _normalize_str("A cat") == "cat"
    assert _normalize_str("An apple") == "apple"

def test_normalize_collapses_whitespace():
    assert _normalize_str("hello   world\n") == "hello world"

def test_normalize_handles_empty():
    assert _normalize_str("") == ""
    assert _normalize_str("   ") == ""
```

- [ ] **Step 2: Run, expect ImportError**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_grader.py -v`

- [ ] **Step 3: Implement**

```python
# eval/grading/gaia_grader.py
"""GAIA scoring — ported from the official evaluation harness.

Reference: gaia-benchmark/leaderboard scoring code.
"""
from __future__ import annotations
import re
import string


_ARTICLE_RE = re.compile(r"\b(a|an|the)\b", re.IGNORECASE)
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_WS_RE = re.compile(r"\s+")


def _normalize_str(s: str) -> str:
    """Lower, strip articles + punctuation, collapse whitespace."""
    if not s:
        return ""
    s = s.lower()
    s = _ARTICLE_RE.sub(" ", s)
    s = s.translate(_PUNCT_TABLE)
    s = _WS_RE.sub(" ", s).strip()
    return s
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_grader.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/grading/gaia_grader.py tests/eval/test_gaia_grader.py
git commit -m "feat(eval): _normalize_str (articles + punctuation + whitespace)"
```

### Task 2.2: `_try_float` + `question_scorer` for numbers & strings

**Files:**
- Modify: `tests/eval/test_gaia_grader.py` (append)
- Modify: `eval/grading/gaia_grader.py` (append)

- [ ] **Step 1: Add failing tests**

```python
def test_scorer_exact_string():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("Paris", "Paris") is True
    assert question_scorer("the Eiffel Tower", "Eiffel Tower") is True
    assert question_scorer("London", "Paris") is False

def test_scorer_number_with_tolerance():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("42", "42") is True
    assert question_scorer("42.0", "42") is True
    assert question_scorer("41.999", "42") is True   # within 0.01 rel tol
    assert question_scorer("$1,234.56", "1234.56") is True  # strip currency/commas
    assert question_scorer("100", "200") is False

def test_scorer_robust_to_whitespace():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("  Paris  \n", "Paris") is True
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/grading/gaia_grader.py`:
```python
def _try_float(s: str) -> float | None:
    """Parse 's' as float after stripping $/€/£/¥ and commas. Return None if NA."""
    if not s:
        return None
    cleaned = re.sub(r"[$€£¥,]", "", s).strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def question_scorer(model_answer: str, ground_truth: str) -> bool:
    """Return True iff model_answer matches ground_truth.

    Rules (in order):
      1. If both parse as float: equal within 1% relative tolerance.
      2. If ground_truth contains a comma: treat both as multisets of items,
         normalized element-wise (order-insensitive, exact-element match).
      3. Else: normalized string equality.
    """
    if model_answer is None:
        return False
    gt_f = _try_float(ground_truth)
    ma_f = _try_float(model_answer)
    if gt_f is not None and ma_f is not None:
        if gt_f == 0:
            return abs(ma_f) < 1e-9
        return abs(ma_f - gt_f) / abs(gt_f) <= 0.01

    if "," in ground_truth:
        gt_items = {_normalize_str(x) for x in ground_truth.split(",") if x.strip()}
        ma_items = {_normalize_str(x) for x in model_answer.split(",") if x.strip()}
        return gt_items == ma_items

    return _normalize_str(model_answer) == _normalize_str(ground_truth)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_grader.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/grading/gaia_grader.py tests/eval/test_gaia_grader.py
git commit -m "feat(eval): question_scorer (numeric tol + string normalize)"
```

### Task 2.3: List answer support

**Files:**
- Modify: `tests/eval/test_gaia_grader.py` (append)

- [ ] **Step 1: Add failing tests**

```python
def test_scorer_list_order_insensitive():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("Paris, London", "London, Paris") is True
    assert question_scorer("Paris", "London, Paris") is False  # missing
    assert question_scorer("Paris, London, Berlin", "London, Paris") is False  # extra

def test_scorer_list_with_normalization():
    from eval.grading.gaia_grader import question_scorer
    assert question_scorer("the Apple, an orange", "apple, orange") is True
```

- [ ] **Step 2: Run, expect pass (already implemented in 2.2)**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_grader.py -v`
Expected: 8 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/eval/test_gaia_grader.py
git commit -m "test(eval): pin list-answer scoring behavior"
```

### Task 2.4: `extract_final_answer`

**Files:**
- Modify: `tests/eval/test_gaia_grader.py` (append)
- Modify: `eval/grading/gaia_grader.py` (append)

- [ ] **Step 1: Add failing tests**

```python
def test_extract_explicit_final_answer_marker():
    from eval.grading.gaia_grader import extract_final_answer
    text = "Let me think.\n\nFINAL ANSWER: 42"
    assert extract_final_answer(text) == "42"

def test_extract_case_insensitive_marker():
    from eval.grading.gaia_grader import extract_final_answer
    assert extract_final_answer("final answer: paris") == "paris"
    assert extract_final_answer("Final Answer:  Paris  \n") == "Paris"

def test_extract_fallback_to_last_paragraph():
    from eval.grading.gaia_grader import extract_final_answer
    text = "Step 1: ...\n\nStep 2: ...\n\nThe answer is 42."
    assert extract_final_answer(text) == "The answer is 42."

def test_extract_empty_or_whitespace():
    from eval.grading.gaia_grader import extract_final_answer
    assert extract_final_answer("") == ""
    assert extract_final_answer("   ") == ""
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/grading/gaia_grader.py`:
```python
_FINAL_ANSWER_RE = re.compile(
    r"final\s+answer\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def extract_final_answer(assistant_content: str) -> str:
    """Prefer 'FINAL ANSWER: X' (case-insensitive); else return last paragraph.

    Returns empty string for empty input.
    """
    if not assistant_content or not assistant_content.strip():
        return ""
    m = _FINAL_ANSWER_RE.search(assistant_content)
    if m:
        return m.group(1).strip()
    # Fallback: last non-empty paragraph
    paragraphs = [p.strip() for p in assistant_content.split("\n\n") if p.strip()]
    return paragraphs[-1] if paragraphs else ""
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_gaia_grader.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/grading/gaia_grader.py tests/eval/test_gaia_grader.py
git commit -m "feat(eval): extract_final_answer (marker + fallback)"
```

---

## Phase 3 — Metrics schema + collector (`eval/metrics/`)

### Task 3.1: Dataclass schemas

**Files:**
- Test: `tests/eval/test_metrics_schema.py`
- Create: `eval/metrics/schema.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/eval/test_metrics_schema.py
from dataclasses import asdict
from eval.metrics.schema import (
    IterSnapshot, TaskMetrics, SessionMetrics, ComparisonReport,
)

def test_iter_snapshot_serializes_to_dict():
    s = IterSnapshot(
        iter_index=0, bucket_system_prompt=100, bucket_user_input=20,
        bucket_tool_calls=0, bucket_llm_output=50,
        bucket_tool_definitions=200, bucket_summary=0,
        total_tokens=370, ratio=0.0037, compaction_tier="NONE",
        tokens_saved_this_iter=0,
    )
    d = asdict(s)
    assert d["compaction_tier"] == "NONE"
    assert d["total_tokens"] == 370

def test_task_metrics_defaults_for_master_branch():
    """Master branch has no compaction — fields default to 0/empty."""
    tm = TaskMetrics(
        task_id="t1", task_index=0, level=1, branch="master",
        final_answer="42", ground_truth="42", is_correct=True,
        failed=False, failure_reason=None, per_iter_snapshots=[],
        bucket_system_prompt=0, bucket_user_input=0, bucket_tool_calls=0,
        bucket_llm_output=0, bucket_tool_definitions=0, bucket_summary=0,
        peak_total_tokens=0, peak_ratio=0.0, overflow=False,
        compactions_in_task=0, tier1_count=0, tier2_count=0, tier3_count=0,
        tokens_saved_in_task=0, summarize_llm_overhead_tokens=0,
        api_prompt_tokens=0, api_completion_tokens=0, api_total_tokens=0,
        iter_count=0, wall_time_seconds=0.0,
    )
    assert tm.branch == "master"
    assert tm.compactions_in_task == 0

def test_session_metrics_aggregate_fields():
    sm = SessionMetrics(
        branch="cc", started_at="2026-06-14T10:00:00", finished_at="...",
        git_commit="abc123", config_snapshot={"context_window": 200000},
        tasks_total=30, tasks_correct=18, tasks_failed=0, tasks_tool_unavailable=0,
        accuracy=0.6, peak_total_tokens_overall=500000, peak_ratio_overall=0.5,
        overflow_count=0, compactions_total=10, tier1_total=8, tier2_total=2,
        tier3_total=0, tokens_saved_total=12000, summarize_llm_overhead_total=0,
        peak_ratio_p50=0.2, peak_ratio_p95=0.45, tokens_saved_p50=100,
        tokens_saved_p95=2000, api_total_tokens_sum=1_000_000, iter_count_sum=200,
        wall_time_seconds_total=600.0,
    )
    assert sm.accuracy == 0.6
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

```python
# eval/metrics/schema.py
"""Dataclasses for per-task / per-session / cross-branch metrics."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class IterSnapshot:
    iter_index: int
    bucket_system_prompt: int
    bucket_user_input: int
    bucket_tool_calls: int
    bucket_llm_output: int
    bucket_tool_definitions: int
    bucket_summary: int
    total_tokens: int
    ratio: float
    compaction_tier: str           # "NONE" | "SNIP" | "PRUNE" | "SUMMARIZE"
    tokens_saved_this_iter: int


@dataclass
class TaskMetrics:
    # Identity
    task_id: str
    task_index: int
    level: int
    branch: str

    # Outcome
    final_answer: str
    ground_truth: str
    is_correct: bool
    failed: bool
    failure_reason: str | None
        # context_overflow | llm_error | rate_limit | max_iter
        # | tool_unavailable | grader_error
    per_iter_snapshots: list[IterSnapshot] = field(default_factory=list)

    # End-of-task bucket totals
    bucket_system_prompt: int = 0
    bucket_user_input: int = 0
    bucket_tool_calls: int = 0
    bucket_llm_output: int = 0
    bucket_tool_definitions: int = 0
    bucket_summary: int = 0
    peak_total_tokens: int = 0
    peak_ratio: float = 0.0
    overflow: bool = False

    # Compaction (always zero on master)
    compactions_in_task: int = 0
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0
    tokens_saved_in_task: int = 0
    summarize_llm_overhead_tokens: int = 0

    # Cost / latency
    api_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    iter_count: int = 0
    wall_time_seconds: float = 0.0


@dataclass
class SessionMetrics:
    branch: str
    started_at: str
    finished_at: str
    git_commit: str
    config_snapshot: dict

    tasks_total: int
    tasks_correct: int
    tasks_failed: int
    tasks_tool_unavailable: int
    accuracy: float

    peak_total_tokens_overall: int
    peak_ratio_overall: float
    overflow_count: int

    compactions_total: int
    tier1_total: int
    tier2_total: int
    tier3_total: int
    tokens_saved_total: int
    summarize_llm_overhead_total: int

    peak_ratio_p50: float
    peak_ratio_p95: float
    tokens_saved_p50: int
    tokens_saved_p95: int

    api_total_tokens_sum: int
    iter_count_sum: int
    wall_time_seconds_total: float


@dataclass
class ComparisonReport:
    master: SessionMetrics
    cc: SessionMetrics
    accuracy_delta: float
    peak_ratio_delta: float
    api_tokens_delta: int
    api_tokens_delta_pct: float
    overflow_delta: int
    per_task_diffs: list[dict] = field(default_factory=list)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_metrics_schema.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/metrics/schema.py tests/eval/test_metrics_schema.py
git commit -m "feat(eval): metric dataclass schemas"
```

### Task 3.2: `collect_task_metrics` for context-compaction branch

**Files:**
- Test: `tests/eval/test_collector.py`
- Create: `eval/metrics/collector.py`

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_collector.py
from cc_harness.tokens import TurnTokenStats
from cc_harness.context import CompactionStats, CompactionTier
from eval.datasets.gaia_loader import GaiaTask
from eval.metrics.collector import collect_task_metrics
from eval.metrics.schema import IterSnapshot

def _task() -> GaiaTask:
    return GaiaTask("t1", "q", 1, "42", None)

def test_collect_with_compaction_stats():
    """Branch that has compaction populates tier counts + tokens_saved."""
    comp = CompactionStats(
        tier=CompactionTier.SNIP,
        before_tokens=1000, after_tokens=600,
        ratio_before=0.6, ratio_after=0.36,
        messages_snip=2,
    )
    stats = TurnTokenStats(
        user_input=100, tool_calls=50, llm_output=80,
        system_prompt=200, tool_definitions=170, summary=0,
        api_prompt_tokens=600, api_completion_tokens=100, api_total_tokens=700,
        iter_count=3, compaction=comp, api_reported=True,
    )
    snapshots = [
        IterSnapshot(
            iter_index=0, bucket_system_prompt=200, bucket_user_input=100,
            bucket_tool_calls=0, bucket_llm_output=0, bucket_tool_definitions=170,
            bucket_summary=0, total_tokens=470, ratio=0.0047,
            compaction_tier="NONE", tokens_saved_this_iter=0,
        ),
        IterSnapshot(
            iter_index=1, bucket_system_prompt=200, bucket_user_input=100,
            bucket_tool_calls=300, bucket_llm_output=80, bucket_tool_definitions=170,
            bucket_summary=0, total_tokens=850, ratio=0.0085,
            compaction_tier="SNIP", tokens_saved_this_iter=400,
        ),
    ]
    tm = collect_task_metrics(
        task=_task(), task_index=0, branch="context-compaction",
        turn_stats=stats, iter_snapshots=snapshots,
        final_answer="42", is_correct=True, failed=False, failure_reason=None,
        wall_time_seconds=12.5, context_window=200_000,
    )
    assert tm.tier1_count == 1
    assert tm.compactions_in_task == 1
    assert tm.tokens_saved_in_task == 400
    assert tm.peak_total_tokens == 850
    assert tm.api_total_tokens == 700
    assert tm.bucket_user_input == 100
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

```python
# eval/metrics/collector.py
"""Build TaskMetrics from TurnTokenStats + per-iter snapshots."""
from __future__ import annotations
from eval.datasets.gaia_loader import GaiaTask
from eval.metrics.schema import IterSnapshot, TaskMetrics


def collect_task_metrics(
    *,
    task: GaiaTask,
    task_index: int,
    branch: str,
    turn_stats,                       # TurnTokenStats; duck-typed for master compat
    iter_snapshots: list[IterSnapshot],
    final_answer: str,
    is_correct: bool,
    failed: bool,
    failure_reason: str | None,
    wall_time_seconds: float,
    context_window: int,
) -> TaskMetrics:
    """Pure function. Handles missing `compaction` field on master.

    `turn_stats` may be either branch's TurnTokenStats; we use getattr for any
    field that exists only on context-compaction.
    """
    peak = max((s.total_tokens for s in iter_snapshots), default=0)
    peak_ratio = peak / context_window if context_window else 0.0
    overflow = peak_ratio > 1.0

    # Aggregate compaction across iters from snapshots (works for both branches;
    # master snapshots will all be NONE)
    tier1 = sum(1 for s in iter_snapshots if s.compaction_tier == "SNIP")
    tier2 = sum(1 for s in iter_snapshots if s.compaction_tier == "PRUNE")
    tier3 = sum(1 for s in iter_snapshots if s.compaction_tier == "SUMMARIZE")
    tokens_saved = sum(s.tokens_saved_this_iter for s in iter_snapshots)
    compactions = tier1 + tier2 + tier3

    return TaskMetrics(
        task_id=task.task_id, task_index=task_index, level=task.level, branch=branch,
        final_answer=final_answer, ground_truth=task.ground_truth,
        is_correct=is_correct, failed=failed, failure_reason=failure_reason,
        per_iter_snapshots=iter_snapshots,
        bucket_system_prompt=getattr(turn_stats, "system_prompt", 0),
        bucket_user_input=getattr(turn_stats, "user_input", 0),
        bucket_tool_calls=getattr(turn_stats, "tool_calls", 0),
        bucket_llm_output=getattr(turn_stats, "llm_output", 0),
        bucket_tool_definitions=getattr(turn_stats, "tool_definitions", 0),
        bucket_summary=getattr(turn_stats, "summary", 0),
        peak_total_tokens=peak, peak_ratio=peak_ratio, overflow=overflow,
        compactions_in_task=compactions,
        tier1_count=tier1, tier2_count=tier2, tier3_count=tier3,
        tokens_saved_in_task=tokens_saved,
        summarize_llm_overhead_tokens=0,  # populated by reconstruct (3.4)
        api_prompt_tokens=getattr(turn_stats, "api_prompt_tokens", 0),
        api_completion_tokens=getattr(turn_stats, "api_completion_tokens", 0),
        api_total_tokens=getattr(turn_stats, "api_total_tokens", 0),
        iter_count=getattr(turn_stats, "iter_count", 0),
        wall_time_seconds=wall_time_seconds,
    )
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_collector.py -v`

- [ ] **Step 5: Commit**

```bash
git add eval/metrics/collector.py tests/eval/test_collector.py
git commit -m "feat(eval): collect_task_metrics (per-iter snapshot aggregation)"
```

### Task 3.3: `collect_task_metrics` handles master shape (no compaction)

**Files:**
- Modify: `tests/eval/test_collector.py` (append)

- [ ] **Step 1: Add failing test**

```python
def test_collect_handles_master_without_compaction_field():
    """Master's TurnTokenStats has no 'compaction' / no 'summary' bucket.

    We simulate by passing a SimpleNamespace lacking those fields.
    """
    from types import SimpleNamespace
    stats = SimpleNamespace(
        user_input=100, tool_calls=50, llm_output=80,
        system_prompt=200, tool_definitions=170,
        # no `summary`, no `compaction`
        api_prompt_tokens=600, api_completion_tokens=100, api_total_tokens=700,
        iter_count=3, api_reported=True,
    )
    snapshots = [IterSnapshot(
        iter_index=0, bucket_system_prompt=200, bucket_user_input=100,
        bucket_tool_calls=0, bucket_llm_output=0, bucket_tool_definitions=170,
        bucket_summary=0, total_tokens=470, ratio=0.005,
        compaction_tier="NONE", tokens_saved_this_iter=0,
    )]
    tm = collect_task_metrics(
        task=_task(), task_index=0, branch="master",
        turn_stats=stats, iter_snapshots=snapshots,
        final_answer="42", is_correct=True, failed=False, failure_reason=None,
        wall_time_seconds=5.0, context_window=200_000,
    )
    assert tm.bucket_summary == 0
    assert tm.tier1_count == 0
    assert tm.compactions_in_task == 0
    assert tm.api_total_tokens == 700
```

- [ ] **Step 2: Run, expect pass (impl already handles via getattr)**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_collector.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/eval/test_collector.py
git commit -m "test(eval): pin master-shape compatibility for collector"
```

### Task 3.4: `reconstruct_iter_snapshots` (post-hoc message replay)

**Files:**
- Test: `tests/eval/test_collector.py` (append)
- Modify: `eval/metrics/collector.py` (append)

- [ ] **Step 1: Add failing test**

```python
def test_reconstruct_snapshots_from_messages_only():
    """Master branch: no per-iter compaction data. Snapshots derived from
    walking messages and categorizing prefix-by-prefix.
    """
    from cc_harness.tokens import TokenCounter
    from eval.metrics.collector import reconstruct_iter_snapshots

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "thinking",
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
        {"role": "assistant", "content": "answer"},
    ]
    counter = TokenCounter()
    snaps = reconstruct_iter_snapshots(
        messages=messages, tools=[], counter=counter,
        compaction_per_iter=[],     # master: empty
        context_window=200_000,
        prefix_before_task=2,       # system + user already in
    )
    # 2 assistant boundaries → 2 snapshots
    assert len(snaps) == 2
    # ratios increase
    assert snaps[1].total_tokens >= snaps[0].total_tokens
    assert all(s.compaction_tier == "NONE" for s in snaps)
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

Append to `eval/metrics/collector.py`:
```python
def reconstruct_iter_snapshots(
    *,
    messages: list[dict],
    tools: list[dict] | None,
    counter,                            # TokenCounter
    compaction_per_iter: list,          # list[CompactionStats]; [] for master
    context_window: int,
    prefix_before_task: int,
) -> list[IterSnapshot]:
    """Walk messages from prefix_before_task forward; emit one snapshot per
    assistant-message boundary (representing one ReAct iter completion).

    For each assistant boundary, categorize the prefix-so-far into 6 buckets
    via `counter.categorize`. If compaction_per_iter has an entry for that
    iter, tier + tokens_saved come from it; else NONE / 0.
    """
    snapshots: list[IterSnapshot] = []
    iter_idx = 0
    for end_idx in range(prefix_before_task + 1, len(messages) + 1):
        if messages[end_idx - 1].get("role") != "assistant":
            continue
        cats = counter.categorize(messages[:end_idx], tools=tools)
        total = sum(cats.values())
        # compaction stat for this iter (if any)
        if iter_idx < len(compaction_per_iter):
            comp = compaction_per_iter[iter_idx]
            tier_name = comp.tier.name if comp.tier else "NONE"
            saved = max(0, comp.before_tokens - comp.after_tokens)
        else:
            tier_name, saved = "NONE", 0
        snapshots.append(IterSnapshot(
            iter_index=iter_idx,
            bucket_system_prompt=cats.get("system_prompt", 0),
            bucket_user_input=cats.get("user_input", 0),
            bucket_tool_calls=cats.get("tool_calls", 0),
            bucket_llm_output=cats.get("llm_output", 0),
            bucket_tool_definitions=cats.get("tool_definitions", 0),
            bucket_summary=cats.get("summary", 0),
            total_tokens=total,
            ratio=total / context_window if context_window else 0.0,
            compaction_tier=tier_name,
            tokens_saved_this_iter=saved,
        ))
        iter_idx += 1
    return snapshots
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_collector.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/metrics/collector.py tests/eval/test_collector.py
git commit -m "feat(eval): reconstruct_iter_snapshots post-hoc replay"
```

### Task 3.5: `aggregate_session_metrics`

**Files:**
- Test: `tests/eval/test_aggregate.py`
- Modify: `eval/metrics/collector.py` (append)

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_aggregate.py
from eval.metrics.collector import aggregate_session_metrics
from eval.metrics.schema import TaskMetrics


def _tm(**overrides):
    defaults = dict(
        task_id="t", task_index=0, level=1, branch="cc",
        final_answer="", ground_truth="", is_correct=True,
        failed=False, failure_reason=None,
    )
    defaults.update(overrides)
    return TaskMetrics(**defaults)

def test_aggregate_basic_counts():
    tms = [
        _tm(task_id="t1", is_correct=True, peak_total_tokens=100,
            peak_ratio=0.001, tier1_count=1, tokens_saved_in_task=50,
            api_total_tokens=300, iter_count=3, wall_time_seconds=5.0),
        _tm(task_id="t2", is_correct=False, peak_total_tokens=200,
            peak_ratio=0.002, tier2_count=1, tokens_saved_in_task=80,
            api_total_tokens=400, iter_count=4, wall_time_seconds=7.0),
    ]
    sm = aggregate_session_metrics(
        tms, branch="cc", started_at="t0", finished_at="t1",
        git_commit="sha", config_snapshot={"context_window": 200_000},
        tool_unavailable_count=0,
    )
    assert sm.tasks_total == 2
    assert sm.tasks_correct == 1
    assert sm.tasks_failed == 0
    assert sm.accuracy == 0.5
    assert sm.peak_total_tokens_overall == 200
    assert sm.tier1_total == 1
    assert sm.tier2_total == 1
    assert sm.tokens_saved_total == 130
    assert sm.api_total_tokens_sum == 700
    assert sm.wall_time_seconds_total == 12.0

def test_aggregate_excludes_tool_unavailable_from_accuracy():
    tms = [
        _tm(task_id="t1", is_correct=True),
        _tm(task_id="t2", is_correct=False),
    ]
    sm = aggregate_session_metrics(
        tms, branch="cc", started_at="t0", finished_at="t1",
        git_commit="sha", config_snapshot={},
        tool_unavailable_count=5,
    )
    # 1 correct / (2 runnable) = 0.5; the 5 unavail are tracked separately
    assert sm.accuracy == 0.5
    assert sm.tasks_tool_unavailable == 5
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

Append to `eval/metrics/collector.py`:
```python
import statistics


def aggregate_session_metrics(
    task_metrics: list[TaskMetrics], *,
    branch: str, started_at: str, finished_at: str, git_commit: str,
    config_snapshot: dict, tool_unavailable_count: int,
):
    from eval.metrics.schema import SessionMetrics  # local: avoid circular

    n = len(task_metrics)
    correct = sum(1 for t in task_metrics if t.is_correct)
    failed = sum(1 for t in task_metrics if t.failed)
    runnable = n  # tool_unavailable excluded BEFORE this fn; n is the runnable pool
    accuracy = (correct / runnable) if runnable else 0.0

    def _q(vals, q):
        return statistics.quantiles(vals, n=100)[q - 1] if len(vals) >= 2 else (vals[0] if vals else 0)

    peak_ratios = [t.peak_ratio for t in task_metrics] or [0.0]
    saved = [t.tokens_saved_in_task for t in task_metrics] or [0]
    peaks = [t.peak_total_tokens for t in task_metrics] or [0]

    return SessionMetrics(
        branch=branch, started_at=started_at, finished_at=finished_at,
        git_commit=git_commit, config_snapshot=config_snapshot,
        tasks_total=n, tasks_correct=correct, tasks_failed=failed,
        tasks_tool_unavailable=tool_unavailable_count, accuracy=accuracy,
        peak_total_tokens_overall=max(peaks),
        peak_ratio_overall=max(peak_ratios),
        overflow_count=sum(1 for t in task_metrics if t.overflow),
        compactions_total=sum(t.compactions_in_task for t in task_metrics),
        tier1_total=sum(t.tier1_count for t in task_metrics),
        tier2_total=sum(t.tier2_count for t in task_metrics),
        tier3_total=sum(t.tier3_count for t in task_metrics),
        tokens_saved_total=sum(saved),
        summarize_llm_overhead_total=sum(t.summarize_llm_overhead_tokens for t in task_metrics),
        peak_ratio_p50=_q(peak_ratios, 50), peak_ratio_p95=_q(peak_ratios, 95),
        tokens_saved_p50=int(_q(saved, 50)), tokens_saved_p95=int(_q(saved, 95)),
        api_total_tokens_sum=sum(t.api_total_tokens for t in task_metrics),
        iter_count_sum=sum(t.iter_count for t in task_metrics),
        wall_time_seconds_total=sum(t.wall_time_seconds for t in task_metrics),
    )
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_aggregate.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/metrics/collector.py tests/eval/test_aggregate.py
git commit -m "feat(eval): aggregate_session_metrics (counts + p50/p95)"
```

### Task 3.6: `compare_sessions`

**Files:**
- Modify: `tests/eval/test_aggregate.py` (append)
- Modify: `eval/metrics/collector.py` (append)

- [ ] **Step 1: Write failing test**

```python
def test_compare_sessions_computes_deltas():
    from eval.metrics.collector import compare_sessions, aggregate_session_metrics
    master = aggregate_session_metrics(
        [_tm(task_id="t1", is_correct=True, api_total_tokens=1000,
             peak_total_tokens=500, peak_ratio=0.25, overflow=False)],
        branch="master", started_at="", finished_at="",
        git_commit="m", config_snapshot={}, tool_unavailable_count=0,
    )
    cc = aggregate_session_metrics(
        [_tm(task_id="t1", is_correct=True, api_total_tokens=600,
             peak_total_tokens=300, peak_ratio=0.15, overflow=False)],
        branch="cc", started_at="", finished_at="",
        git_commit="c", config_snapshot={}, tool_unavailable_count=0,
    )
    cmp = compare_sessions(master, cc)
    assert cmp.api_tokens_delta == -400  # cc saved 400
    assert cmp.api_tokens_delta_pct == -40.0
    assert cmp.peak_ratio_delta == cc.peak_ratio_overall - master.peak_ratio_overall
    assert len(cmp.per_task_diffs) == 1
    assert cmp.per_task_diffs[0]["task_id"] == "t1"
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

Append to `eval/metrics/collector.py`:
```python
def compare_sessions(master, cc):
    from eval.metrics.schema import ComparisonReport

    api_delta = cc.api_total_tokens_sum - master.api_total_tokens_sum
    api_pct = (100.0 * api_delta / master.api_total_tokens_sum) if master.api_total_tokens_sum else 0.0
    # Per-task diff is built by run.py orchestrator (which has both task lists);
    # if not pre-populated, leave empty
    return ComparisonReport(
        master=master, cc=cc,
        accuracy_delta=cc.accuracy - master.accuracy,
        peak_ratio_delta=cc.peak_ratio_overall - master.peak_ratio_overall,
        api_tokens_delta=api_delta, api_tokens_delta_pct=api_pct,
        overflow_delta=cc.overflow_count - master.overflow_count,
        per_task_diffs=[],  # populated externally; see Task 6.4
    )


def build_per_task_diffs(
    master_tms: list[TaskMetrics], cc_tms: list[TaskMetrics],
) -> list[dict]:
    """Pair task metrics by task_id; emit one dict per pair (or singleton if one branch missing)."""
    by_id_master = {t.task_id: t for t in master_tms}
    by_id_cc = {t.task_id: t for t in cc_tms}
    all_ids = sorted(by_id_master.keys() | by_id_cc.keys())
    out = []
    for tid in all_ids:
        m = by_id_master.get(tid)
        c = by_id_cc.get(tid)
        out.append({
            "task_id": tid,
            "level": (m or c).level,
            "master_correct": m.is_correct if m else None,
            "cc_correct": c.is_correct if c else None,
            "master_failed": m.failed if m else None,
            "cc_failed": c.failed if c else None,
            "master_peak": m.peak_total_tokens if m else None,
            "cc_peak": c.peak_total_tokens if c else None,
            "master_api_tokens": m.api_total_tokens if m else None,
            "cc_api_tokens": c.api_total_tokens if c else None,
        })
    return out
```

Add to test:
```python
def test_build_per_task_diffs():
    from eval.metrics.collector import build_per_task_diffs
    m = [_tm(task_id="t1", is_correct=True, peak_total_tokens=500)]
    c = [_tm(task_id="t1", is_correct=False, peak_total_tokens=300)]
    diffs = build_per_task_diffs(m, c)
    assert len(diffs) == 1
    assert diffs[0]["master_correct"] is True
    assert diffs[0]["cc_correct"] is False
    assert diffs[0]["master_peak"] == 500
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_aggregate.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/metrics/collector.py tests/eval/test_aggregate.py
git commit -m "feat(eval): compare_sessions + build_per_task_diffs"
```

---

## Phase 4 — Session runner (`eval/runners/session_runner.py`)

The only stateful module. Wires LLM + MCP + `run_turn` inside a single worktree, loops over tasks, captures metrics. Heavy use of `inspect.signature` to stay compatible with master's older `run_turn` signature.

### Task 4.1: `_branch_supports_context_config` helper

**Files:**
- Test: `tests/eval/test_session_runner.py`
- Create: `eval/runners/session_runner.py`

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_session_runner.py
import inspect
from eval.runners.session_runner import _branch_supports_context_config

def test_supports_context_config_true_when_param_present():
    async def fake_run_turn(messages, llm, mcp, *, context_config=None): ...
    assert _branch_supports_context_config(fake_run_turn) is True

def test_supports_context_config_false_on_master_shape():
    async def fake_run_turn(messages, llm, mcp): ...
    assert _branch_supports_context_config(fake_run_turn) is False
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

```python
# eval/runners/session_runner.py
"""Multi-turn single-session GAIA runner, per worktree."""
from __future__ import annotations
import inspect


def _branch_supports_context_config(run_turn_fn) -> bool:
    """Inspect the worktree's run_turn signature for `context_config` kwarg."""
    try:
        sig = inspect.signature(run_turn_fn)
    except (TypeError, ValueError):
        return False
    return "context_config" in sig.parameters
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_session_runner.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/runners/session_runner.py tests/eval/test_session_runner.py
git commit -m "feat(eval): _branch_supports_context_config introspection"
```

### Task 4.2: Worktree-aware `cc_harness` importer

**Files:**
- Modify: `tests/eval/test_session_runner.py` (append)
- Modify: `eval/runners/session_runner.py` (append)

- [ ] **Step 1: Write failing test**

```python
def test_worktree_import_context_restores_sys_path(tmp_path):
    """import_from_worktree must remove the prepended path on exit."""
    import sys
    from eval.runners.session_runner import import_from_worktree

    # Fake worktree with a tiny module
    (tmp_path / "fakepkg").mkdir()
    (tmp_path / "fakepkg" / "__init__.py").write_text("FOO = 1")

    sys_path_before = list(sys.path)
    with import_from_worktree(tmp_path) as mods:
        import fakepkg
        assert fakepkg.FOO == 1
        assert str(tmp_path) in sys.path
    # After exit
    assert sys.path == sys_path_before
    # Module is also removed from cache so re-import would re-read
    assert "fakepkg" not in sys.modules
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/runners/session_runner.py`:
```python
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def import_from_worktree(worktree_path: Path):
    """Prepend worktree to sys.path; pop newly imported modules on exit.

    Yields a dict of (module_name -> module) imported while inside.
    """
    worktree_str = str(worktree_path)
    sys.path.insert(0, worktree_str)
    before = set(sys.modules)
    imported: dict = {}
    try:
        yield imported
    finally:
        # Remove modules imported during the with-block
        new_modules = set(sys.modules) - before
        for name in new_modules:
            sys.modules.pop(name, None)
            imported[name] = None
        # Pop our path entry (only the first occurrence we added)
        try:
            sys.path.remove(worktree_str)
        except ValueError:
            pass
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_session_runner.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/runners/session_runner.py tests/eval/test_session_runner.py
git commit -m "feat(eval): import_from_worktree context manager"
```

### Task 4.3: `_extract_compaction_per_iter` from a TurnTokenStats

**Files:**
- Modify: `tests/eval/test_session_runner.py` (append)
- Modify: `eval/runners/session_runner.py` (append)

Note: `TurnTokenStats.compaction` is the LAST iter's stats only, not a list. We need a different capture path. Plan: monkey-patch `maybe_compact` on the worktree's `cc_harness.context` module to collect every call's stats into a session-scoped list before delegating.

- [ ] **Step 1: Write failing test**

```python
def test_compaction_capture_list_collects_each_iter_stats(monkeypatch):
    """Verify the capture wrapper around maybe_compact appends stats."""
    from eval.runners.session_runner import make_compaction_capture
    captured = []

    async def fake_maybe_compact(*a, **kw):
        from cc_harness.context import CompactionStats, CompactionTier
        return CompactionStats(
            tier=CompactionTier.SNIP, before_tokens=100, after_tokens=50,
            ratio_before=0.5, ratio_after=0.25,
        )
    wrapped = make_compaction_capture(fake_maybe_compact, captured)

    import asyncio
    asyncio.run(wrapped("messages", "tools", "counter", "config", "llm"))
    asyncio.run(wrapped("messages", "tools", "counter", "config", "llm"))
    assert len(captured) == 2
    assert all(s.tier.name == "SNIP" for s in captured)
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/runners/session_runner.py`:
```python
def make_compaction_capture(orig_maybe_compact, captured: list):
    """Wrap maybe_compact so each call's CompactionStats is appended to `captured`.

    Pass `captured.clear()` between tasks to scope per-task. The wrapper is
    installed by monkey-patching `cc_harness.context.maybe_compact` after
    importing it from the worktree (so the worktree's agent.py picks up the
    wrapped version).
    """
    async def wrapped(*args, **kwargs):
        stats = await orig_maybe_compact(*args, **kwargs)
        captured.append(stats)
        return stats
    return wrapped
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_session_runner.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/runners/session_runner.py tests/eval/test_session_runner.py
git commit -m "feat(eval): make_compaction_capture wrapper"
```

### Task 4.4: `_classify_failure` from an exception

**Files:**
- Modify: `tests/eval/test_session_runner.py` (append)
- Modify: `eval/runners/session_runner.py` (append)

- [ ] **Step 1: Write failing tests**

```python
def test_classify_failure_context_overflow():
    from eval.runners.session_runner import classify_failure
    assert classify_failure(Exception("context_length_exceeded: too long")) == "context_overflow"
    assert classify_failure(Exception("maximum context length is 200000")) == "context_overflow"

def test_classify_failure_rate_limit():
    from eval.runners.session_runner import classify_failure
    assert classify_failure(Exception("429 Too Many Requests")) == "rate_limit"
    assert classify_failure(Exception("rate limit exceeded")) == "rate_limit"

def test_classify_failure_other():
    from eval.runners.session_runner import classify_failure
    assert classify_failure(Exception("random network error")) == "llm_error"
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/runners/session_runner.py`:
```python
import re as _re

_CTX_OVERFLOW_RE = _re.compile(
    r"context.{0,5}(length|window|size).{0,15}(exceed|max|too long|long)",
    _re.IGNORECASE,
)
_RATE_LIMIT_RE = _re.compile(r"\b(429|rate.{0,3}limit)\b", _re.IGNORECASE)


def classify_failure(exc: BaseException) -> str:
    msg = str(exc)
    if _CTX_OVERFLOW_RE.search(msg):
        return "context_overflow"
    if _RATE_LIMIT_RE.search(msg):
        return "rate_limit"
    return "llm_error"
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_session_runner.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/runners/session_runner.py tests/eval/test_session_runner.py
git commit -m "feat(eval): classify_failure regex-based"
```

### Task 4.5: `run_session` — the main loop (TDD with FakeLLM)

**Files:**
- Modify: `tests/eval/test_session_runner.py` (append; reuse FakeLLM/FakeMCP from test_agent.py)
- Modify: `eval/runners/session_runner.py` (append)

- [ ] **Step 1: Write failing test for happy path (3 tasks, all succeed)**

```python
import pytest
from pathlib import Path
from cc_harness.llm import PendingToolCall
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
from cc_harness.mcp_client import ToolResult


def _final_event(text):
    return [FakeStreamEvent(kind="content", text=text),
            FakeStreamEvent(kind="done", content=text, pending=[], finish_reason="stop")]


@pytest.mark.asyncio
async def test_run_session_happy_path_3_tasks(tmp_path):
    from eval.datasets.gaia_loader import GaiaTask
    from eval.runners.session_runner import run_session
    from cc_harness.config import ContextConfig

    tasks = [
        GaiaTask("t1", "What is 2+2?", 1, "4", None),
        GaiaTask("t2", "Capital of France?", 1, "Paris", None),
        GaiaTask("t3", "Sum of 1..10?", 1, "55", None),
    ]
    llm = FakeLLM(responses=[
        _final_event("FINAL ANSWER: 4"),
        _final_event("FINAL ANSWER: Paris"),
        _final_event("FINAL ANSWER: 55"),
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    result = await run_session(
        tasks=tasks,
        llm=llm, mcp=mcp,
        branch="test", out_dir=tmp_path,
        context_config=ContextConfig(enabled=False),
        max_iter=5, checkpoint_every=2,
        abort_after_overflows=3,
    )
    assert result.tasks_total == 3
    assert result.tasks_correct == 3
    assert result.accuracy == 1.0
    # trace.jsonl exists
    assert (tmp_path / "trace.jsonl").exists()
    # messages.json checkpoint exists (after task 2)
    assert (tmp_path / "messages.json").exists()
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement minimum to pass**

Append to `eval/runners/session_runner.py`:
```python
import json
import time
from datetime import datetime, timezone
from eval.datasets.gaia_loader import GaiaTask
from eval.grading.gaia_grader import question_scorer, extract_final_answer
from eval.metrics.collector import (
    collect_task_metrics, reconstruct_iter_snapshots, aggregate_session_metrics,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run_session(
    *,
    tasks: list[GaiaTask],
    llm,
    mcp,
    branch: str,
    out_dir: Path,
    context_config,                    # ContextConfig
    max_iter: int = 20,
    checkpoint_every: int = 5,
    abort_after_overflows: int = 3,
    git_commit: str = "unknown",
    cwd: str | None = None,
):
    """Drive a multi-turn session through cc_harness.agent.run_turn.

    Assumes `cc_harness` is already importable (caller arranged sys.path / worktree).
    Writes:
      - out_dir / trace.jsonl     (one TaskMetrics-as-dict per line)
      - out_dir / messages.json   (periodic checkpoint, final state at end)
      - out_dir / session_metrics.json
    Returns: SessionMetrics
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict

    from cc_harness.agent import run_turn
    from cc_harness.tokens import TokenCounter
    from cc_harness.prompts import build_system_prompt
    from cc_harness import context as _ctx_mod

    counter = TokenCounter()
    cc_supported = _branch_supports_context_config(run_turn)

    # Install compaction capture (only meaningful on context-compaction branch)
    captured_per_task: list = []
    if cc_supported and getattr(_ctx_mod, "maybe_compact", None):
        original = _ctx_mod.maybe_compact
        _ctx_mod.maybe_compact = make_compaction_capture(original, captured_per_task)

    messages: list[dict] = [{
        "role": "system",
        "content": build_system_prompt(cwd or ".", mode="coding"),
    }]
    started_at = _now_iso()
    consecutive_overflows = 0
    task_metrics: list = []
    trace_path = out_dir / "trace.jsonl"
    msgs_path = out_dir / "messages.json"

    for idx, task in enumerate(tasks):
        captured_per_task.clear()
        messages.append({"role": "user", "content": task.question})
        prefix = len(messages)
        t_start = time.time()
        failed, failure_reason = False, None
        turn_stats = None
        try:
            kw = {}
            if cc_supported:
                kw["context_config"] = context_config
            turn_stats = await run_turn(
                messages, llm, mcp, max_iter=max_iter, cwd=cwd or ".",
                token_counter=counter, **kw,
            )
        except Exception as e:
            failed = True
            failure_reason = classify_failure(e)
            if failure_reason == "context_overflow":
                consecutive_overflows += 1
            else:
                consecutive_overflows = 0

        wall = time.time() - t_start
        last_asst = next(
            (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        answer_text = (last_asst or {}).get("content", "") or ""
        answer = extract_final_answer(answer_text)
        try:
            correct = bool(answer) and question_scorer(answer, task.ground_truth)
        except Exception:
            correct = False
            failure_reason = failure_reason or "grader_error"

        snaps = reconstruct_iter_snapshots(
            messages=messages, tools=None, counter=counter,
            compaction_per_iter=list(captured_per_task),
            context_window=context_config.context_window,
            prefix_before_task=prefix,
        )
        if turn_stats is None:
            from types import SimpleNamespace
            turn_stats = SimpleNamespace(
                user_input=0, tool_calls=0, llm_output=0, system_prompt=0,
                tool_definitions=0, summary=0,
                api_prompt_tokens=0, api_completion_tokens=0, api_total_tokens=0,
                iter_count=0, api_reported=False,
            )
        tm = collect_task_metrics(
            task=task, task_index=idx, branch=branch,
            turn_stats=turn_stats, iter_snapshots=snaps,
            final_answer=answer, is_correct=correct,
            failed=failed, failure_reason=failure_reason,
            wall_time_seconds=wall, context_window=context_config.context_window,
        )
        task_metrics.append(tm)
        # Append jsonl
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(tm), ensure_ascii=False) + "\n")
        # Checkpoint messages periodically
        if (idx + 1) % checkpoint_every == 0:
            msgs_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if abort_after_overflows and consecutive_overflows >= abort_after_overflows:
            break

    # Final messages dump
    msgs_path.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    finished_at = _now_iso()

    sm = aggregate_session_metrics(
        task_metrics, branch=branch,
        started_at=started_at, finished_at=finished_at,
        git_commit=git_commit,
        config_snapshot={
            "context_window": context_config.context_window,
            "tier1_threshold": context_config.tier1_threshold,
            "tier2_threshold": context_config.tier2_threshold,
            "tier3_threshold": context_config.tier3_threshold,
            "protect_zone_tokens": context_config.protect_zone_tokens,
            "enabled": context_config.enabled,
        },
        tool_unavailable_count=0,
    )
    (out_dir / "session_metrics.json").write_text(
        json.dumps(asdict(sm), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sm
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_session_runner.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/runners/session_runner.py tests/eval/test_session_runner.py
git commit -m "feat(eval): run_session main loop (FakeLLM happy path)"
```

### Task 4.6: `run_session` failure paths (overflow, abort_after_overflows)

**Files:**
- Modify: `tests/eval/test_session_runner.py` (append)

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_run_session_continues_after_one_failure(tmp_path):
    from eval.datasets.gaia_loader import GaiaTask
    from eval.runners.session_runner import run_session
    from cc_harness.config import ContextConfig

    tasks = [
        GaiaTask("t1", "ok", 1, "4", None),
        GaiaTask("t2", "fail", 1, "x", None),
        GaiaTask("t3", "ok again", 1, "9", None),
    ]
    # 3 turns: success, raise, success
    class FailingFakeLLM:
        def __init__(self):
            self.call_count = 0
            self.model = "fake"
        async def chat(self, messages, tools):
            i = self.call_count
            self.call_count += 1
            if i == 1:
                raise Exception("random network error")
            text = "FINAL ANSWER: 4" if i == 0 else "FINAL ANSWER: 9"
            for ev in [FakeStreamEvent(kind="content", text=text),
                       FakeStreamEvent(kind="done", content=text, pending=[],
                                       finish_reason="stop")]:
                yield ev
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    sm = await run_session(
        tasks=tasks, llm=FailingFakeLLM(), mcp=mcp,
        branch="test", out_dir=tmp_path,
        context_config=ContextConfig(enabled=False),
        max_iter=3, checkpoint_every=5, abort_after_overflows=3,
    )
    assert sm.tasks_total == 3
    assert sm.tasks_failed == 1


@pytest.mark.asyncio
async def test_run_session_aborts_after_consecutive_overflows(tmp_path):
    from eval.datasets.gaia_loader import GaiaTask
    from eval.runners.session_runner import run_session
    from cc_harness.config import ContextConfig

    tasks = [
        GaiaTask(f"t{i}", f"q{i}", 1, "x", None) for i in range(5)
    ]
    class OverflowFakeLLM:
        def __init__(self):
            self.call_count = 0
            self.model = "fake"
        async def chat(self, messages, tools):
            self.call_count += 1
            raise Exception("context_length_exceeded: messages too long")
            yield  # unreachable; needed to make it a generator

    sm = await run_session(
        tasks=tasks, llm=OverflowFakeLLM(),
        mcp=FakeMCP(tools_spec=[], results={}, calls=[]),
        branch="test", out_dir=tmp_path,
        context_config=ContextConfig(enabled=False),
        max_iter=3, checkpoint_every=5, abort_after_overflows=3,
    )
    # Hit 3 overflows in a row → break before all 5 tasks
    assert sm.tasks_total == 3
    assert sm.overflow_count == 0  # peak_ratio not set (no snapshots), but failed yes
    assert sm.tasks_failed == 3
```

- [ ] **Step 2: Run, expect pass (logic already implemented in 4.5)**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_session_runner.py -v`
Expected: 10 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/eval/test_session_runner.py
git commit -m "test(eval): run_session failure-continue + overflow-abort"
```

---

## Phase 5 — Reports (`eval/reports/markdown.py`)

### Task 5.1: `render_comparison_report` markdown emit

**Files:**
- Test: `tests/eval/test_markdown_report.py`
- Create: `eval/reports/markdown.py`

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_markdown_report.py
from eval.metrics.schema import SessionMetrics, ComparisonReport
from eval.reports.markdown import render_comparison_report


def _sm(**overrides):
    defaults = dict(
        branch="b", started_at="2026-06-14T10:00:00Z",
        finished_at="2026-06-14T10:05:00Z",
        git_commit="abc1234", config_snapshot={"context_window": 200000},
        tasks_total=30, tasks_correct=20, tasks_failed=0,
        tasks_tool_unavailable=0, accuracy=0.6667,
        peak_total_tokens_overall=500_000, peak_ratio_overall=2.5,
        overflow_count=0, compactions_total=10, tier1_total=8,
        tier2_total=2, tier3_total=0, tokens_saved_total=12000,
        summarize_llm_overhead_total=0, peak_ratio_p50=1.2,
        peak_ratio_p95=2.3, tokens_saved_p50=100, tokens_saved_p95=2000,
        api_total_tokens_sum=1_000_000, iter_count_sum=200,
        wall_time_seconds_total=600.0,
    )
    defaults.update(overrides)
    return SessionMetrics(**defaults)


def test_report_header_and_summary_table_present():
    cmp = ComparisonReport(
        master=_sm(branch="master", accuracy=0.6, overflow_count=5,
                   api_total_tokens_sum=1_500_000),
        cc=_sm(branch="context-compaction", accuracy=0.55, overflow_count=0,
               api_total_tokens_sum=1_000_000),
        accuracy_delta=-0.05, peak_ratio_delta=-1.0,
        api_tokens_delta=-500_000, api_tokens_delta_pct=-33.3,
        overflow_delta=-5, per_task_diffs=[],
    )
    md = render_comparison_report(cmp)
    assert "# GAIA Context Eval" in md
    assert "TL;DR" in md
    assert "Accuracy" in md
    assert "**−33.3%**" in md or "-33.3%" in md
    assert "master" in md and "context-compaction" in md
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

```python
# eval/reports/markdown.py
"""Render ComparisonReport to GitHub-flavored Markdown."""
from __future__ import annotations
from eval.metrics.schema import ComparisonReport


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def render_comparison_report(cmp: ComparisonReport) -> str:
    m, c = cmp.master, cmp.cc
    cfg = m.config_snapshot
    parts: list[str] = []
    parts.append(f"# GAIA Context Eval — {m.started_at} → {m.finished_at}")
    parts.append("")
    parts.append(f"**Config**: context_window={cfg.get('context_window')}, "
                 f"tiers={cfg.get('tier1_threshold')}/{cfg.get('tier2_threshold')}/{cfg.get('tier3_threshold')}, "
                 f"protect={cfg.get('protect_zone_tokens')}")
    parts.append(f"**Commits**: master={m.git_commit} · cc={c.git_commit}")
    parts.append("")
    parts.append("## TL;DR")
    parts.append("")
    parts.append("| Metric | master | context-compaction | Δ |")
    parts.append("|---|---:|---:|---:|")
    parts.append(f"| Accuracy | {m.tasks_correct}/{m.tasks_total} ({m.accuracy:.1%}) | "
                 f"{c.tasks_correct}/{c.tasks_total} ({c.accuracy:.1%}) | "
                 f"**{cmp.accuracy_delta:+.2%}** |")
    parts.append(f"| Tasks failed | {m.tasks_failed} | {c.tasks_failed} | "
                 f"**{c.tasks_failed - m.tasks_failed:+d}** |")
    parts.append(f"| Context overflows | {m.overflow_count} | {c.overflow_count} | "
                 f"**{cmp.overflow_delta:+d}** |")
    parts.append(f"| Peak ratio (overall) | {m.peak_ratio_overall:.2f} | {c.peak_ratio_overall:.2f} | "
                 f"**{cmp.peak_ratio_delta:+.2f}** |")
    parts.append(f"| API tokens total | {_fmt_int(m.api_total_tokens_sum)} | {_fmt_int(c.api_total_tokens_sum)} | "
                 f"**{_pct(cmp.api_tokens_delta_pct)}** |")
    parts.append(f"| Wall time (s) | {m.wall_time_seconds_total:.0f} | {c.wall_time_seconds_total:.0f} | "
                 f"**{c.wall_time_seconds_total - m.wall_time_seconds_total:+.0f}** |")
    parts.append("")
    parts.append("## Context dynamics")
    parts.append("")
    parts.append(f"- Tier 1 (Snip): master={m.tier1_total}, cc={c.tier1_total}")
    parts.append(f"- Tier 2 (Prune): master={m.tier2_total}, cc={c.tier2_total}")
    parts.append(f"- Tier 3 (Summarize): master={m.tier3_total}, cc={c.tier3_total}")
    parts.append(f"- Tokens saved (total): master={_fmt_int(m.tokens_saved_total)}, cc={_fmt_int(c.tokens_saved_total)}")
    parts.append(f"- Summarize LLM overhead: cc={_fmt_int(c.summarize_llm_overhead_total)} tokens")
    parts.append("")
    parts.append("## Per-task accuracy diff")
    parts.append("")
    parts.append("<details><summary>Show all tasks ({} rows)</summary>".format(len(cmp.per_task_diffs)))
    parts.append("")
    parts.append("| task_id | level | master | cc | master_peak | cc_peak |")
    parts.append("|---|---|---|---|---:|---:|")
    for d in cmp.per_task_diffs:
        m_mark = "✓" if d.get("master_correct") else ("✗" if d.get("master_failed") is False else "—")
        c_mark = "✓" if d.get("cc_correct") else ("✗" if d.get("cc_failed") is False else "—")
        parts.append(f"| {d['task_id'][:8]} | {d['level']} | {m_mark} | {c_mark} | "
                     f"{_fmt_int(d.get('master_peak') or 0)} | {_fmt_int(d.get('cc_peak') or 0)} |")
    parts.append("</details>")
    parts.append("")
    return "\n".join(parts)
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_markdown_report.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/reports/markdown.py tests/eval/test_markdown_report.py
git commit -m "feat(eval): render_comparison_report markdown"
```

### Task 5.2: CSV emit for spreadsheet pivoting

**Files:**
- Test: `tests/eval/test_csv_report.py`
- Create: `eval/reports/csv_report.py`

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_csv_report.py
import csv
from io import StringIO
from eval.metrics.schema import SessionMetrics, ComparisonReport
from eval.reports.csv_report import write_csv_report
from tests.eval.test_markdown_report import _sm


def test_csv_has_expected_columns(tmp_path):
    cmp = ComparisonReport(
        master=_sm(branch="master"),
        cc=_sm(branch="context-compaction"),
        accuracy_delta=0.0, peak_ratio_delta=0.0,
        api_tokens_delta=0, api_tokens_delta_pct=0.0,
        overflow_delta=0,
        per_task_diffs=[
            {"task_id": "t1", "level": 1, "master_correct": True,
             "cc_correct": False, "master_peak": 100, "cc_peak": 200,
             "master_failed": False, "cc_failed": False,
             "master_api_tokens": 300, "cc_api_tokens": 250},
        ],
    )
    p = tmp_path / "report.csv"
    write_csv_report(cmp, p)
    rows = list(csv.DictReader(p.open()))
    assert len(rows) == 1
    assert rows[0]["task_id"] == "t1"
    assert rows[0]["master_correct"] == "True"
    assert rows[0]["cc_correct"] == "False"
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

```python
# eval/reports/csv_report.py
"""Emit per-task comparison rows as CSV for spreadsheet pivot work."""
from __future__ import annotations
import csv
from pathlib import Path
from eval.metrics.schema import ComparisonReport


def write_csv_report(cmp: ComparisonReport, path: Path) -> None:
    if not cmp.per_task_diffs:
        path.write_text("", encoding="utf-8")
        return
    keys = list(cmp.per_task_diffs[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in cmp.per_task_diffs:
            w.writerow(row)
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_csv_report.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/reports/csv_report.py tests/eval/test_csv_report.py
git commit -m "feat(eval): write_csv_report per-task pivot CSV"
```

---

## Phase 6 — CLI orchestrator (`eval/run.py`)

### Task 6.1: argparse + Args dataclass

**Files:**
- Test: `tests/eval/test_run_cli.py`
- Create: `eval/run.py`

- [ ] **Step 1: Write failing test**

```python
# tests/eval/test_run_cli.py
from eval.run import parse_args


def test_defaults():
    a = parse_args([])
    assert a.level == "1"
    assert a.limit == 30
    assert a.seed == 42
    assert a.include_attachments is True
    assert a.branches == ["master", "context-compaction"]
    assert a.checkpoint_every == 5
    assert a.abort_after_overflows == 3
    assert a.on_error == "continue"
    assert a.parallel is False
    assert a.dry_run is False
    assert a.report_format == ["markdown", "csv", "json"]
    assert a.context_window is None  # use ContextConfig default


def test_overrides():
    a = parse_args([
        "--level", "all", "--limit", "100", "--seed", "7",
        "--branches", "master", "--worktree-dir", "/tmp/wt",
        "--context-window", "32000", "--parallel", "--dry-run",
    ])
    assert a.level == "all"
    assert a.limit == 100
    assert a.branches == ["master"]
    assert a.context_window == 32000
    assert a.parallel is True
    assert a.dry_run is True


def test_limit_below_30_rejected():
    import pytest
    with pytest.raises(SystemExit):
        parse_args(["--limit", "10"])
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement**

```python
# eval/run.py
"""CLI orchestrator for the GAIA context-management eval."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Args:
    level: str
    limit: int
    seed: int
    include_attachments: bool
    branches: list[str]
    worktree_dir: Path
    keep_worktrees: bool
    mcp_config: Path
    context_window: int | None
    tier_overrides: str | None
    max_iter: int
    parallel: bool
    on_error: str
    checkpoint_every: int
    abort_after_overflows: int
    dry_run: bool
    output_dir: Path | None
    report_format: list[str]
    no_report: bool


def _csv(v: str) -> list[str]:
    return [s.strip() for s in v.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> Args:
    p = argparse.ArgumentParser(prog="eval.run",
                                description="GAIA context-management A/B eval")
    p.add_argument("--level", default="1", choices=["1", "2", "3", "all"])
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--include-attachments", type=lambda v: v.lower() != "false", default=True)
    p.add_argument("--branches", type=_csv, default=["master", "context-compaction"])
    p.add_argument("--worktree-dir", type=Path, default=Path(".eval-worktrees"))
    p.add_argument("--keep-worktrees", action="store_true")
    p.add_argument("--mcp-config", type=Path, default=Path("mcp.json"))
    p.add_argument("--context-window", type=int, default=None)
    p.add_argument("--tier-overrides", default=None,
                   help='e.g. "1=0.05,2=0.10,3=0.15"')
    p.add_argument("--max-iter", type=int, default=20)
    p.add_argument("--parallel", action="store_true")
    p.add_argument("--on-error", default="continue", choices=["continue", "abort"])
    p.add_argument("--checkpoint-every", type=int, default=5)
    p.add_argument("--abort-after-overflows", type=int, default=3)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--report-format", type=_csv, default=["markdown", "csv", "json"])
    p.add_argument("--no-report", action="store_true")
    ns = p.parse_args(argv)
    if ns.limit < 30:
        p.error("--limit must be >= 30 for meaningful report")
    return Args(**vars(ns))
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_run_cli.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/run.py tests/eval/test_run_cli.py
git commit -m "feat(eval): CLI argparse + Args dataclass"
```

### Task 6.2: Preflight checks

**Files:**
- Modify: `tests/eval/test_run_cli.py` (append, with monkeypatch)
- Modify: `eval/run.py` (append)

- [ ] **Step 1: Write failing tests**

```python
def test_preflight_blocks_when_limit_below_30():
    """Already covered by parse_args; this is just to enumerate."""
    # see test_limit_below_30_rejected above

def test_preflight_collects_issues(monkeypatch, tmp_path):
    from eval.run import preflight, Args
    args = Args(
        level="1", limit=30, seed=42, include_attachments=True,
        branches=["master", "context-compaction"],
        worktree_dir=tmp_path / "wt", keep_worktrees=False,
        mcp_config=tmp_path / "nonexistent.json",  # bad
        context_window=None, tier_overrides=None, max_iter=20,
        parallel=False, on_error="continue", checkpoint_every=5,
        abort_after_overflows=3, dry_run=False, output_dir=None,
        report_format=["markdown"], no_report=False,
    )
    # Stub out git/HF checks for unit test
    monkeypatch.setattr("eval.run._git_status_clean", lambda: True)
    monkeypatch.setattr("eval.run._branch_exists", lambda b: True)
    monkeypatch.setattr("eval.run._hf_logged_in", lambda: True)
    issues = preflight(args)
    # mcp_config missing -> blocking issue
    assert any("mcp" in i.lower() for i in issues)
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/run.py`:
```python
import subprocess


def _git_status_clean() -> bool:
    try:
        r = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip() == ""
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _branch_exists(name: str) -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--verify", name],
                           capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _hf_logged_in() -> bool:
    import os
    if os.getenv("HF_TOKEN"):
        return True
    try:
        from huggingface_hub import HfApi
        HfApi().whoami()
        return True
    except Exception:
        return False


def preflight(args: "Args") -> list[str]:
    issues: list[str] = []
    if not _git_status_clean():
        issues.append("uncommitted changes — please commit/stash before running")
    for b in args.branches:
        if not _branch_exists(b):
            issues.append(f"branch not found: {b!r}")
    if not _hf_logged_in():
        issues.append("HuggingFace not logged in (HF_TOKEN env or `huggingface-cli login`)")
    if not args.mcp_config.exists():
        issues.append(f"mcp config not found: {args.mcp_config}")
    return issues
```

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_run_cli.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/run.py tests/eval/test_run_cli.py
git commit -m "feat(eval): preflight checks (git/HF/MCP/sample)"
```

### Task 6.3: Worktree helpers

**Files:**
- Modify: `tests/eval/test_run_cli.py` (append)
- Modify: `eval/run.py` (append)

- [ ] **Step 1: Write failing test (uses real git in tmp_path)**

```python
def test_worktree_add_and_remove(tmp_path, monkeypatch):
    """End-to-end: init a tiny repo, add+remove a worktree."""
    import subprocess as sp
    from eval.run import worktree_add, worktree_remove

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "x.txt").write_text("hi")
    sp.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    wt = repo / "wt"
    monkeypatch.chdir(repo)
    worktree_add(wt, "main")
    assert wt.exists()
    assert (wt / "x.txt").read_text() == "hi"
    worktree_remove(wt)
    assert not wt.exists()
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/run.py`:
```python
def worktree_add(path: Path, branch: str) -> None:
    subprocess.run(["git", "worktree", "add", str(path), branch],
                   check=True, capture_output=True)


def worktree_remove(path: Path, *, force: bool = True) -> None:
    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    try:
        subprocess.run(args, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # Best-effort cleanup; don't crash the eval over leftover worktree
        pass
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_run_cli.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/run.py tests/eval/test_run_cli.py
git commit -m "feat(eval): worktree_add/remove via git CLI"
```

### Task 6.4: `main()` orchestrator (dry-run mode first)

**Files:**
- Modify: `tests/eval/test_run_cli.py` (append)
- Modify: `eval/run.py` (append)

- [ ] **Step 1: Write failing test for --dry-run**

```python
def test_dry_run_does_not_call_llm(monkeypatch, tmp_path, capsys):
    from eval.run import main, Args
    from eval.datasets.gaia_loader import GaiaTask

    fake_tasks = [GaiaTask(f"t{i}", "q", 1, "a", None) for i in range(50)]
    monkeypatch.setattr("eval.run.load_gaia_validation", lambda: fake_tasks)
    monkeypatch.setattr("eval.run._git_status_clean", lambda: True)
    monkeypatch.setattr("eval.run._branch_exists", lambda b: True)
    monkeypatch.setattr("eval.run._hf_logged_in", lambda: True)
    args = Args(
        level="1", limit=30, seed=42, include_attachments=True,
        branches=["master"], worktree_dir=tmp_path / "wt",
        keep_worktrees=True, mcp_config=tmp_path,  # any existing path
        context_window=None, tier_overrides=None, max_iter=20,
        parallel=False, on_error="continue", checkpoint_every=5,
        abort_after_overflows=3, dry_run=True,
        output_dir=tmp_path / "out", report_format=["markdown"],
        no_report=False,
    )
    rc = main(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run" in out or "dry" in out.lower()
    assert "30" in out  # mentions task count
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

Append to `eval/run.py`:
```python
import sys as _sys
from eval.datasets.gaia_loader import (
    load_gaia_validation, filter_tasks, stratified_sample,
)


def main(args: "Args | None" = None) -> int:
    if args is None:
        args = parse_args(_sys.argv[1:])
    print(f"[preflight] checking…")
    issues = preflight(args)
    blocking = [i for i in issues if not i.startswith("⚠")]
    for i in issues:
        print(f"  - {i}")
    if blocking:
        print(f"[preflight] FAIL — {len(blocking)} blocking issue(s)")
        return 2

    print("[dataset] loading GAIA validation…")
    all_tasks = load_gaia_validation()
    if args.level != "all":
        all_tasks = [t for t in all_tasks if t.level == int(args.level)]
    runnable, skipped = filter_tasks(all_tasks, include_attachments=args.include_attachments)
    sample = stratified_sample(runnable, limit=args.limit, seed=args.seed)
    print(f"[dataset] {len(all_tasks)} total → {len(runnable)} runnable → "
          f"{len(sample)} sampled (skipped {len(skipped)} for tool-gap)")

    if args.dry_run:
        print("[Dry run] would execute:")
        for b in args.branches:
            print(f"  branch {b}: {len(sample)} tasks")
        for t in sample[:10]:
            print(f"    - {t.task_id[:8]} L{t.level}: {t.question[:60]}…")
        if len(sample) > 10:
            print(f"    … and {len(sample) - 10} more")
        return 0

    # (Full run path implemented in Task 6.5)
    raise NotImplementedError("full run wired in Task 6.5")


if __name__ == "__main__":
    _sys.exit(main())
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_run_cli.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/run.py tests/eval/test_run_cli.py
git commit -m "feat(eval): main() orchestrator + dry-run mode"
```

### Task 6.5: Full run path (worktree → session → compare → report)

**Files:**
- Modify: `eval/run.py` (replace NotImplementedError block)
- Test: end-to-end smoke deferred to Phase 7

- [ ] **Step 1: Replace the NotImplementedError block**

```python
    # Full run path
    from datetime import datetime, timezone
    import json as _json
    from dataclasses import asdict
    from cc_harness.config import ContextConfig
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.config import load_config
    from eval.runners.session_runner import (
        run_session, import_from_worktree,
    )
    from eval.metrics.collector import compare_sessions, build_per_task_diffs
    from eval.reports.markdown import render_comparison_report
    from eval.reports.csv_report import write_csv_report

    # Resolve output dir
    if args.output_dir is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        args.output_dir = Path("eval/runs") / f"{date}-L{args.level}-{args.limit}q-{head}"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Build ContextConfig
    ctx_kwargs = {}
    if args.context_window:
        ctx_kwargs["context_window"] = args.context_window
    if args.tier_overrides:
        for pair in args.tier_overrides.split(","):
            k, v = pair.split("=")
            ctx_kwargs[f"tier{k.strip()}_threshold"] = float(v.strip())
    ctx_config = ContextConfig(**ctx_kwargs) if ctx_kwargs else ContextConfig()

    # Setup worktrees
    args.worktree_dir.mkdir(parents=True, exist_ok=True)
    branch_to_wt: dict[str, Path] = {}
    for branch in args.branches:
        wt = args.worktree_dir / branch
        if not wt.exists():
            print(f"[worktree] adding {branch} → {wt}")
            worktree_add(wt, branch)
        branch_to_wt[branch] = wt

    # Run each branch SERIALLY (parallel disabled in v1)
    branch_sessions: dict[str, "SessionMetrics"] = {}
    branch_task_metrics: dict[str, list] = {}
    for branch, wt in branch_to_wt.items():
        print(f"[branch] {branch} → {wt}")
        out_dir = args.output_dir / branch
        out_dir.mkdir(parents=True, exist_ok=True)
        commit = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()[:7]
        # Use the worktree's own load_config / LLMClient / MCPClient
        with import_from_worktree(wt):
            from cc_harness.config import load_config as _load_cfg
            from cc_harness.llm import LLMClient as _LLM
            from cc_harness.mcp_client import MCPClient as _MCP
            cfg = _load_cfg(env_path=wt / ".env", mcp_json_path=args.mcp_config)
            llm = _LLM(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url,
                       model=cfg.openai_model)
            mcp = await _MCP.create(cfg.mcp_servers)  # noqa: F821 — context handled outside await
            try:
                sm = await run_session(
                    tasks=sample, llm=llm, mcp=mcp,
                    branch=branch, out_dir=out_dir,
                    context_config=ctx_config, max_iter=args.max_iter,
                    checkpoint_every=args.checkpoint_every,
                    abort_after_overflows=args.abort_after_overflows,
                    git_commit=commit, cwd=str(wt),
                )
            finally:
                await mcp.close()
        branch_sessions[branch] = sm
        # Re-read trace to collect TaskMetrics list for per_task_diffs
        tms = []
        with (out_dir / "trace.jsonl").open(encoding="utf-8") as f:
            for line in f:
                d = _json.loads(line)
                # ignore per_iter_snapshots for diff (large)
                d.pop("per_iter_snapshots", None)
                from eval.metrics.schema import TaskMetrics
                tms.append(TaskMetrics(**d, per_iter_snapshots=[]))
        branch_task_metrics[branch] = tms

    # Compare
    if "master" in branch_sessions and "context-compaction" in branch_sessions:
        cmp = compare_sessions(
            branch_sessions["master"], branch_sessions["context-compaction"],
        )
        cmp.per_task_diffs = build_per_task_diffs(
            branch_task_metrics["master"],
            branch_task_metrics["context-compaction"],
        )
        (args.output_dir / "comparison.json").write_text(
            _json.dumps(asdict(cmp), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.no_report and "markdown" in args.report_format:
            (args.output_dir / "report.md").write_text(
                render_comparison_report(cmp), encoding="utf-8",
            )
        if not args.no_report and "csv" in args.report_format:
            write_csv_report(cmp, args.output_dir / "report.csv")
        print(f"[report] written to {args.output_dir}")

    # Cleanup
    if not args.keep_worktrees:
        for branch, wt in branch_to_wt.items():
            worktree_remove(wt)
    return 0
```

Note: `main` must be `async def` since it awaits. Wrap with `asyncio.run` in `__main__`:

```python
if __name__ == "__main__":
    import asyncio as _asyncio
    _sys.exit(_asyncio.run(main()))
```

- [ ] **Step 2: Convert `main` to async**

Change `def main(...)` to `async def main(...)`. The dry-run path returns early (no await), but should still work as a coroutine — callers must `asyncio.run` it.

Update test `test_dry_run_does_not_call_llm`:
```python
import asyncio
rc = asyncio.run(main(args))
```

- [ ] **Step 3: Run tests, expect pass**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/test_run_cli.py -v`
Expected: 6 passed.

- [ ] **Step 4: Verify ruff clean**

Run: `.venv/Scripts/python.exe -m ruff check eval/ tests/eval/`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add eval/run.py tests/eval/test_run_cli.py
git commit -m "feat(eval): full run path (worktree → session → compare → report)"
```

---

## Phase 7 — Docs, smoke test, verification

### Task 7.1: `eval/README.md`

**Files:**
- Create: `eval/README.md`

- [ ] **Step 1: Write README**

```markdown
# eval/

Programmatic A/B evaluation of cc-harness on the GAIA benchmark, comparing
the `master` and `context-compaction` git branches.

## Quick start

```bash
# 1. Install eval extras (one-time)
.venv/Scripts/python.exe -m pip install -e ".[eval,dev]"

# 2. HuggingFace login (one-time)
huggingface-cli login   # or set HF_TOKEN env var

# 3. Dry run (no LLM calls — verifies dataset + preflight)
.venv/Scripts/python.exe -m eval.run --dry-run

# 4. Real run (default: Level 1, 30 questions, both branches)
.venv/Scripts/python.exe -m eval.run

# 5. Output
ls eval/runs/<date>-L1-30q-<sha>/
```

## CLI

See `python -m eval.run --help` for the full surface.
Most useful flags:

- `--level {1,2,3,all}` — GAIA difficulty
- `--limit N` — sample size (minimum 30)
- `--seed N` — deterministic sampling
- `--branches master,context-compaction` — what to compare
- `--context-window N` — override CONTEXT_WINDOW (default: per .env / 200K)
- `--dry-run` — no LLM calls

## What's measured

11 metrics dimensions across 3 categories, per [the design spec](../docs/superpowers/specs/2026-06-14-gaia-context-eval-design.md):

- **Context** — peak tokens, overflow count, compaction tier counts, tokens saved
- **Quality** — GAIA accuracy (correct / runnable)
- **Cost** — API tokens, ReAct iter count, wall time

Outputs to `eval/runs/<run-id>/`:
- `report.md` — human-readable comparison
- `report.csv` — per-task pivot rows
- `comparison.json` — full machine-readable comparison
- `{master,context-compaction}/trace.jsonl` — per-task metrics
- `{master,context-compaction}/messages.json` — final session messages
- `{master,context-compaction}/session_metrics.json` — branch summary

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests/eval/ -v
```

E2E smoke (real LLM, costs money): `_test_e2e_smoke.py` — not collected by default.
```

- [ ] **Step 2: Commit**

```bash
git add eval/README.md
git commit -m "docs(eval): README quickstart + flag reference"
```

### Task 7.2: `_test_e2e_smoke.py` (manual, real-LLM, 3 tasks)

**Files:**
- Create: `tests/eval/_test_e2e_smoke.py`

- [ ] **Step 1: Write smoke test**

```python
"""End-to-end smoke for eval pipeline.

NOT collected by pytest default (underscore prefix). Run manually:

    .venv/Scripts/python.exe -m pytest tests/eval/_test_e2e_smoke.py -v -s

Costs real LLM tokens (~50K, ~1分钱 on DeepSeek). Requires HF login.
"""
import asyncio
import pytest
from pathlib import Path
from eval.run import main, Args


@pytest.mark.asyncio
async def test_e2e_3_task_smoke(tmp_path):
    args = Args(
        level="1", limit=3, seed=42, include_attachments=False,
        branches=["context-compaction"],  # single branch to halve cost
        worktree_dir=tmp_path / "wt", keep_worktrees=True,
        mcp_config=Path("mcp.json"),
        context_window=None, tier_overrides=None, max_iter=10,
        parallel=False, on_error="continue", checkpoint_every=2,
        abort_after_overflows=3, dry_run=False,
        output_dir=tmp_path / "out", report_format=["markdown", "json"],
        no_report=False,
    )
    # Bypass the >=30 limit guard for smoke (Args was built directly, not via parse_args)
    rc = await main(args)
    assert rc == 0
    assert (tmp_path / "out" / "context-compaction" / "session_metrics.json").exists()
    assert (tmp_path / "out" / "context-compaction" / "trace.jsonl").exists()
```

- [ ] **Step 2: Run manually (single branch, 3 tasks; expect 1-2 minutes)**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/_test_e2e_smoke.py -v -s`
Expected: 1 passed.

(If something breaks here, this is the most valuable place to find it — real LLM, real MCP, real workspace.)

- [ ] **Step 3: Commit**

```bash
git add tests/eval/_test_e2e_smoke.py
git commit -m "test(eval): _test_e2e_smoke (real LLM, 3-task)"
```

### Task 7.3: Update root README + CLAUDE.md

**Files:**
- Modify: `README.md` (add eval section)
- Modify: `CLAUDE.md` (add eval section under "Common commands")

- [ ] **Step 1: Append to CLAUDE.md `Common commands` section**

```bash
# GAIA context-management eval (compares master vs context-compaction)
.venv/Scripts/python.exe -m eval.run --dry-run
.venv/Scripts/python.exe -m eval.run --limit 30 --level 1
.venv/Scripts/python.exe -m pytest tests/eval/ -v
```

- [ ] **Step 2: Append a short eval section to README.md**

```markdown
## Evaluation

See `eval/README.md` for the GAIA-based A/B harness comparing context-management
strategies between branches.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: surface eval/ in root README + CLAUDE.md"
```

### Task 7.4: Final verification gate

- [ ] **Step 1: Full test sweep**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all 217 original tests + ~30 new tests pass.

- [ ] **Step 2: Lint**

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/ eval/ tests/`
Expected: no errors.

- [ ] **Step 3: Dry run end-to-end**

Run: `.venv/Scripts/python.exe -m eval.run --dry-run`
Expected: prints task count, no errors, exits 0.

- [ ] **Step 4: Manual smoke (optional but recommended before declaring done)**

Run: `.venv/Scripts/python.exe -m pytest tests/eval/_test_e2e_smoke.py -v -s`
Expected: 1 passed in ~2 min.

- [ ] **Step 5: Final commit (only if not already done by prior tasks)**

```bash
git status   # should be clean
```

---

## Notes for the implementing engineer

- **Reuse `FakeLLM`/`FakeMCP`/`FakeStreamEvent` from `tests/test_agent.py`** — import them, don't re-define. They are public-shaped fixtures.
- **Never modify `cc_harness/`** in any phase. The eval is meant to be a non-invasive observer. The only "patching" we do is the `make_compaction_capture` wrapper on `cc_harness.context.maybe_compact`, applied at session-runner load time.
- **Worktrees can leak** if the test process crashes between `worktree_add` and `worktree_remove`. Use `git worktree prune` to clean up manually if you see stale entries.
- **HF dataset download is one-time ~few MB**, cached under `~/.cache/huggingface/`. After the first run, all dataset access is offline.
- **The 30-task minimum is a soft policy** enforced in `parse_args`. The smoke test bypasses it by constructing `Args` directly, which is fine for testing.
- **`pdf-reader-mcp` URL malformation** (noted in spec §10) will surface as a warn during MCP server boot. Tell the user to fix `mcp.json` if it does.


