# GAIA Context Management Eval — Design

**Status**: Draft (awaiting review)
**Author**: brainstormed with user via `superpowers:brainstorming`
**Date**: 2026-06-14
**Related branches**: `master`, `context-compaction`
**Output location**: `D:\agent_learning\cc-harness\eval\`

---

## 1. Goal

Quantify the impact of cc-harness's 4-tier context compaction (introduced on the
`context-compaction` branch) on real long-running agent sessions, by running
GAIA benchmark tasks through both branches under matched conditions and
comparing:

1. **Context dynamics** — how token usage grows, whether/when compaction
   triggers, how much it saves
2. **Task quality** — does compaction degrade correctness (GAIA grader)?
3. **Cost / latency** — API tokens spent, wall time, ReAct iter count
4. **Robustness** — how many tasks each branch can complete before the session
   collapses (overflow, repeated failures)

The eval is **not** a leaderboard submission; it is an internal A/B that
produces a markdown report for engineering decision-making.

---

## 2. Non-goals

- Submitting to the public GAIA leaderboard
- Optimizing the compaction tiers themselves (this eval informs tuning, doesn't perform it)
- Cross-model comparison (locked to whatever is configured in `.env`, current: `deepseek-v4-flash`)
- Cross-benchmark evaluation (SWE-bench / HumanEval) — the module layout
  leaves room, but only GAIA ships in v1
- Eval-time tuning of `ContextConfig` thresholds (uses what's already on the branch)

---

## 3. Key design decisions (settled in brainstorming)

| # | Decision | Rationale |
|---|---|---|
| D1 | Use HF official `gaia-benchmark/GAIA` validation set | User has access + license accepted |
| D2 | Drive agent **programmatically** via `await run_turn(...)` | Direct access to `TurnTokenStats` (incl. `compaction`); avoids parsing stdout |
| D3 | **Configurable subset**, default 30 questions Level 1 | 30 is the minimum sample for meaningful reporting; user can scale up |
| D4 | Collect **11 metrics dimensions** (context + accuracy + iter + cost) | One-side-only data (context without accuracy) misses whether compaction is "free" |
| D5 | Use **git worktree** to run both branches in parallel directories | Safe, isolates working trees, no checkout thrash mid-run |
| D6 | Keep current `mcp.json` (incl. PDF / Excel / OCR servers); skip purely visual/audio/video tasks via `file_name` suffix filter | PDF/Excel/OCR are covered by user-added MCP servers; image/audio/video remain hard gaps |
| D7 | **Single REPL session, N tasks as consecutive user turns** — `messages` does NOT reset between tasks | Realistic long-conversation scenario; compaction triggers naturally as messages accumulate; no need to artificially shrink `CONTEXT_WINDOW` |
| D8 | Keep `CONTEXT_WINDOW=1048576` (model's real window) by default; allow override via CLI for stress runs | D7 makes compaction fire without faking the budget |
| D9 | Architecture: **pipeline split** into `datasets/ runners/ grading/ metrics/ reports/` packages, not a monolith | Aligns with cc-harness's "small bounded units" style; SWE-bench can be added later by dropping in new loader/grader |
| D10 | `TaskMetrics` includes `per_iter_snapshots` (6-bucket + ratio per ReAct iter) | Enables "context over turns" curve plots; trace volume still under ~1MB for 30 tasks |

---

## 4. Architecture

### 4.1 Module layout

```
eval/
├── __init__.py
├── README.md                       # quickstart, env requirements
│
├── datasets/
│   └── gaia_loader.py              # HF dataset load + file-suffix prefilter + sampling
│
├── runners/
│   └── session_runner.py           # single-worktree single-session N-turn driver
│
├── grading/
│   └── gaia_grader.py              # GAIA official question_scorer + answer extraction
│
├── metrics/
│   ├── schema.py                   # TaskMetrics, IterSnapshot, SessionMetrics,
│   │                               # ComparisonReport dataclasses
│   └── collector.py                # extract metrics from TurnTokenStats + messages diff
│
├── reports/
│   ├── markdown.py                 # render report.md
│   └── (plots.py — Phase 2)
│
├── mcp.eval.json                   # OPTIONAL: trimmed MCP config (no playwright)
├── run.py                          # CLI orchestrator
└── runs/                           # gitignored output
    └── 2026-06-14-30q-L1-<sha>/
        ├── master/
        │   ├── trace.jsonl
        │   ├── messages.json
        │   ├── session_metrics.json
        │   └── stdout.log
        ├── context-compaction/
        │   └── (same)
        ├── comparison.json
        └── report.md
```

### 4.2 Responsibilities per module

- **datasets/gaia_loader.py** — Pure I/O + filter. Loads HF dataset, returns
  `list[GaiaTask]`. Pre-filters out tasks whose `file_name` ends in
  `.png .jpg .jpeg .gif .mp3 .wav .m4a .mp4 .mov .webm` (hard gaps). Tasks with
  PDF/Excel/CSV/TXT/JSON/ZIP file attachments are kept (downloads handled by
  loader on demand, stored in a local cache under `~/.cache/gaia-eval/files/`).
  Stratified sampling by level when `--level all`. `(runnable, skipped)` tuple
  returned so caller can report what was filtered.

- **runners/session_runner.py** — The only stateful module. Takes a list of
  `GaiaTask`, a worktree path, a `ContextConfig`, and an MCP config path. Inside,
  it imports `cc_harness.agent.run_turn` **from the worktree** (via temporary
  `sys.path` prepend), constructs LLM + MCP clients, then loops:
  ```python
  messages = [{"role": "system", "content": build_system_prompt(...)}]
  for i, task in enumerate(tasks):
      messages.append({"role": "user", "content": task.question})
      iter_snapshots = []  # populated via callback or post-hoc inspection
      try:
          turn_stats = await run_turn(
              messages, llm, mcp,
              **maybe_context_config(branch),
          )
      except Exception as e:
          ...record failure, possibly break...
      answer = extract_final_answer(messages[-1]["content"])
      metric = collect_task_metrics(task, turn_stats, iter_snapshots, ...)
      append_jsonl(trace_path, metric)
      if (i + 1) % checkpoint_every == 0:
          dump_messages(messages, checkpoint_path)
  ```
  Failure handling per §6. Returns `SessionMetrics`.

- **grading/gaia_grader.py** — Pure function. `question_scorer(answer,
  ground_truth) -> bool` ports the official GAIA evaluation script (normalization,
  numeric tolerance, list comparison). `extract_final_answer(content)` first
  searches for `FINAL ANSWER:` prefix (GAIA convention); otherwise returns the
  last non-empty paragraph.

- **metrics/collector.py** — Pure function. Given a `GaiaTask`, a
  `TurnTokenStats`, a list of `IterSnapshot`, and the `is_correct` verdict,
  produces a `TaskMetrics`. Handles the fact that master's `TurnTokenStats` has
  no `compaction` field via `getattr(stats, 'compaction', None)`.

- **reports/markdown.py** — Pure function. `render_comparison_report(report:
  ComparisonReport) -> str`. Section layout per §7.

- **run.py** — CLI orchestrator. Argument parsing → preflight checks →
  worktree setup → call `session_runner` per branch (serial by default, parallel
  via `--parallel`) → assemble `ComparisonReport` → write `report.md` →
  worktree cleanup (skip if `--keep-worktrees`).

### 4.3 Data flow per `python -m eval.run`

```
CLI parse ─▶ preflight (git clean / HF login / MCP ping / disk / sample ≥ 30)
            ─▶ gaia_loader.load(level, limit, seed)
                ─▶ tasks: list[GaiaTask]
                ─▶ skipped: list[GaiaTask]  (tool-unavailable)
            ─▶ worktree setup (git worktree add per branch)
            ─▶ for branch in branches:                  # serial by default
                  session_runner.run_session(
                      tasks, worktree[branch], cfg, mcp_path, out[branch]/
                  ) ─▶ SessionMetrics
            ─▶ comparison = compare_sessions(master_sm, cc_sm)
            ─▶ write comparison.json + report.md
            ─▶ worktree cleanup
```

### 4.4 Worktree mechanics

```bash
git worktree add .eval-worktrees/master              master
git worktree add .eval-worktrees/context-compaction  context-compaction
```

- Worktrees live under `.eval-worktrees/` (gitignored, configurable via
  `--worktree-dir`). Each is a full checkout of the corresponding branch.
- `session_runner` does `sys.path.insert(0, str(worktree_path))` before
  importing `cc_harness.*`; removes on exit. This is the safest way to ensure
  each worktree's session uses **its own** `agent.py` / `tokens.py` /
  `context.py`.
- master's `run_turn` does not accept `context_config`. Use
  `inspect.signature(run_turn).parameters` and pass `context_config` only
  if the parameter exists. Same with the `compaction` attribute on
  `TurnTokenStats`.
- Cleanup: `git worktree remove --force <path>` after both sessions complete.
  Skip on `--keep-worktrees`. Cleanup failure does **not** abort — log + leave
  intact.

---

## 5. Metrics schema

### 5.1 `IterSnapshot` (per ReAct iter within a task)

```python
@dataclass
class IterSnapshot:
    iter_index: int                 # 0-based within task
    bucket_system_prompt: int
    bucket_user_input: int
    bucket_tool_calls: int
    bucket_llm_output: int
    bucket_tool_definitions: int
    bucket_summary: int
    total_tokens: int
    ratio: float                    # total / CONTEXT_WINDOW
    compaction_tier: str            # "NONE" | "SNIP" | "PRUNE" | "SUMMARIZE"
    tokens_saved_this_iter: int     # before - after (0 if NONE)
```

Reconstructed **post-hoc** by collector (we do not modify `run_turn`). Source
material: the final `messages` list after the turn + the `turn_stats.compaction`
field (one per iter that triggered compaction). Collector replays bucket
totals iter-by-iter so each snapshot reflects the state at the boundary of
that iter. Live in-loop collection would require either patching `run_turn` or
threading a callback through it; both are rejected to keep eval non-invasive.

Note: master branch has no per-iter compaction stats and no `compaction` field.
For master, `compaction_tier="NONE"` always, and per-iter snapshots are derived
only from message replay (still useful for plotting context growth).

### 5.2 `TaskMetrics` (one per task, appended to trace.jsonl)

```python
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
    failure_reason: str | None       # context_overflow / llm_error / rate_limit
                                     # / max_iter / tool_unavailable / grader_error

    # Per-iter trace
    per_iter_snapshots: list[IterSnapshot]

    # End-of-task bucket totals
    bucket_system_prompt: int
    bucket_user_input: int
    bucket_tool_calls: int
    bucket_llm_output: int
    bucket_tool_definitions: int
    bucket_summary: int              # always 0 on master
    peak_total_tokens: int
    peak_ratio: float
    overflow: bool                   # peak_ratio > 1.0

    # Compaction (always zero/empty on master)
    compactions_in_task: int
    tier1_count: int
    tier2_count: int
    tier3_count: int
    tokens_saved_in_task: int
    summarize_llm_overhead_tokens: int

    # Cost
    api_prompt_tokens: int
    api_completion_tokens: int
    api_total_tokens: int
    iter_count: int
    wall_time_seconds: float
```

### 5.3 `SessionMetrics` (one per branch)

```python
@dataclass
class SessionMetrics:
    branch: str
    started_at: str
    finished_at: str
    git_commit: str
    config_snapshot: dict             # CONTEXT_WINDOW + thresholds + protect_zone

    tasks_total: int
    tasks_correct: int
    tasks_failed: int
    tasks_tool_unavailable: int       # excluded from accuracy denominator
    accuracy: float                   # correct / (total - tool_unavailable)

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
```

### 5.4 `ComparisonReport`

```python
@dataclass
class ComparisonReport:
    master: SessionMetrics
    cc: SessionMetrics

    accuracy_delta: float
    peak_ratio_delta: float
    api_tokens_delta: int
    api_tokens_delta_pct: float
    overflow_delta: int

    per_task_diffs: list[dict]        # aligned by task_id
```

---

## 6. CLI & configuration

```
python -m eval.run [OPTIONS]

# Dataset
--level {1,2,3,all}              default 1
--limit N                        default 30, minimum 30
--seed N                         default 42
--include-attachments {true,false}  default true
                                  (purely visual/audio/video always skipped)

# Branches
--branches LIST                  default master,context-compaction
--worktree-dir PATH              default ./.eval-worktrees
--keep-worktrees

# Agent
--mcp-config PATH                default <repo>/mcp.json
--context-window N               override CONTEXT_WINDOW (else .env / default 1M)
--tier-overrides STR             e.g. "1=0.05,2=0.10,3=0.15" for stress runs
--max-iter N                     default 20

# Execution
--parallel
--on-error {continue,abort}      default continue
--checkpoint-every N             default 5
--abort-after-overflows N        consecutive context_overflow failures that kill
                                  the session; default 3 (use 0 to disable)
--dry-run                        load + ping + list tasks, no LLM calls

# Output
--output-dir PATH                default eval/runs/{YYYY-MM-DD}-{level}-{limit}q-{sha}
--report-format LIST             default markdown,csv,json
--no-report
```

### Preflight checks (abort if any fail; warn for soft issues)

| Check | Severity |
|---|---|
| `git status` clean on both branches | abort |
| Specified branches exist | abort |
| `huggingface-cli` logged in (or `HF_TOKEN` env var) | abort |
| MCP config file exists | abort |
| Output directory writable | abort |
| `--limit` ≥ 30 | abort |
| Each SSE MCP endpoint reachable (ping) | warn (skip that server) |
| Local `pdftotext` / `pandas` available (fallback path) | info |

### Configuration precedence

```
CLI flag > .env (CONTEXT_WINDOW, CONTEXT_TIER*) > ContextConfig defaults
```

`eval/run.py` explicitly constructs a `ContextConfig` from CLI flags and passes
it through to `session_runner`. The runner does NOT let the worktree's
`cc_harness` read its own `.env` for context config — both worktrees use the
same explicit `ContextConfig` for fair comparison.

---

## 7. Failure handling

| Failure | Detection | Action |
|---|---|---|
| Single MCP server fails to start | preflight ping | warn red, continue (tools from that server unavailable) |
| Tool call timeout | agent's existing handler | trace records `tool_error`, task continues |
| LLM rate limit (429) | streaming exception | retry with backoff (3 attempts); on exhaustion: `failed=True, reason=rate_limit`, session continues to next task |
| LLM other error (5xx, network) | streaming exception | same as rate limit |
| Context overflow on master (LLM returns `context_length_exceeded`) | exception text match | `failed=True, reason=context_overflow, overflow=True`. Session continues. If `--abort-after-overflows` consecutive (default 3) tasks overflow: session ends, remaining tasks recorded with `skipped: session_dead` |
| User Ctrl-C | KeyboardInterrupt | dump current trace + messages, render partial report with explicit "TRUNCATED" header |
| Disk write fail | OSError | abort entire session |
| GAIA grader raises | exception | task marked `is_correct=False, grader_warning=str(e)`, session continues |

---

## 8. Report (`report.md`) structure

1. **Header** — config snapshot (model, CONTEXT_WINDOW, thresholds, MCP set)
2. **TL;DR table** — 6-row summary (accuracy, completion, overflow, peak tokens, API tokens, wall time)
3. **Context dynamics** — tier distribution counts, 6-bucket end-state stacked breakdown, peak ratio curve (Phase 2 plot)
4. **Per-task accuracy diff** — table aligned by `task_id`; default folded, showing only regressions + overflows
5. **Failure post-mortem** — categorized failure list per branch; for regressions, a short note pointing at suspected lost context
6. **Reproducibility** — exact CLI invocation, git commits per branch, seed, dataset row IDs

Also emit `report.csv` alongside `.md` for spreadsheet pivoting.

---

## 9. Testing strategy

```
tests/eval/
├── conftest.py                    # FakeLLM, FakeMCP (reuse from tests/)
├── test_gaia_loader.py            # mock HF; verify filter + sampling
├── test_gaia_grader.py            # use GAIA paper sample answers as fixtures
├── test_collector.py              # feed fake TurnTokenStats with/without compaction
├── test_session_runner.py         # FakeLLM 3-task session; verify accumulation,
│                                  # checkpoint, failure-continue, overflow-after-3
├── test_markdown_report.py        # render with fixture SessionMetrics
├── test_run_cli.py                # argparse + preflight
└── _test_e2e_smoke.py             # real LLM; 3-task smoke; underscore = not auto-collected
```

**Key cases:**
- `test_collector` — `TurnTokenStats(compaction=None)` (master shape) must not raise; `bucket_summary=0`
- `test_session_runner` — FakeLLM programmed to raise `context_overflow` on iter 4, then iter 5, then iter 6 → session-dead trigger
- `test_gaia_grader` — port at least 5 sample pairs from GAIA's released eval harness
- **Not tested**: worktree creation (let real git do it), HF download (cache covers)

**Acceptance gate:**
- All `tests/eval/` pass (`pytest tests/eval/`)
- `python -m eval.run --limit 3 --dry-run` exits 0
- `python -m eval.run --limit 3` completes (manual)
- `report.md` previews cleanly in GitHub markdown

---

## 10. Open questions / risks

| Risk | Mitigation |
|---|---|
| `pdf-reader-mcp` URL in `mcp.json` looks malformed (`mcp-.api-inference...`) | preflight pings will surface this; user to fix or remove |
| Cross-task contamination (Q12 answer leaks into Q13 context) | This is the very thing compaction is supposed to mitigate; if it happens, the eval will surface it as a regression |
| Stratified sampling on Level 1 may pick easy questions, masking compaction value | Run with `--level all` for v2 if v1 shows no signal |
| GAIA's `question_scorer` may be strict on whitespace/case | Faithful port from official harness; add `grader_warning` field for transparency |
| 1M `CONTEXT_WINDOW` + 30 tasks may not naturally trigger Tier 3 | If trace shows `tier3_total=0`, rerun with `--tier-overrides "1=0.3,2=0.5,3=0.7"` |

---

## 11. Out of scope (deferred)

- Plots (`reports/plots.py`) — Phase 2, after the markdown report is stable
- Parallel branch execution validation under heavy LLM rate limits — Phase 2
- SWE-bench loader/grader — clear future extension; module layout already accommodates
- Compaction tier tuning based on eval results — separate effort
- Public dashboard / continuous eval CI — not yet justified by one-shot evaluation needs

---
