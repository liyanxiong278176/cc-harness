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
- `--context-window N` — override CONTEXT_WINDOW (default: per .env)
- `--tier-overrides STR` — force-trigger compaction, e.g. `"1=0.05,2=0.10,3=0.15"`
- `--abort-after-overflows N` — kill session after N consecutive overflows (default 3)
- `--dry-run` — no LLM calls

## What's measured

11 metrics dimensions across 3 categories (see [the design spec](../docs/superpowers/specs/2026-06-14-gaia-context-eval-design.md)):

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

## Architecture

```
eval/
├── datasets/gaia_loader.py        # HF load + file-suffix filter + stratified sampling
├── grading/gaia_grader.py          # GAIA scorer (normalize, numeric, list, extract)
├── metrics/
│   ├── schema.py                  # Dataclasses (TaskMetrics, SessionMetrics, …)
│   └── collector.py               # collect / aggregate / compare
├── reports/
│   ├── markdown.py                # render_comparison_report
│   └── csv_report.py              # write_csv_report
├── runners/session_runner.py      # multi-turn driver, worktree-aware import
└── run.py                         # CLI orchestrator
```

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests/eval/ -v
```

E2E smoke (real LLM, costs money): `_test_e2e_smoke.py` — not collected by default.
