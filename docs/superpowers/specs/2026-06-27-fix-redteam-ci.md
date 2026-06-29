# Fix Red Team CI: Spec

**Date:** 2026-06-27
**Branch:** test-red-team
**Status:** PROPOSED

## Problem

The cc-harness security eval (red team) CI is broken in three ways after the OWASP integration:

1. **`main.py not found` (1529 / 1640 probes fail = 93%).** The wrapper at
   `eval/promptfoo/wrappers/cc_harness.py` resolves `MAIN_PY` via
   `Path(__file__).resolve().parents[3] / "main.py"`, which works locally but
   fails in CI. Cause unknown — could be checkout path, parent counting,
   or wrapper invocation context. Result: almost every probe fails before
   reaching the agent.

2. **Hand-written + dynamic attacks don't run.** `promptfoo redteam run`
   only generates attacks from `redteam.plugins` and ignores top-level
   `tests:` references. The current run had `0` hand-written and `0` dynamic
   probes — only OWASP plugin probes ran.

3. **PR comment shows everything as "static".** The JS classifies probes
   with `r.testCase?.metadata?.source === 'dynamic'`. OWASP probes have no
   `source` field, so all 1640 are labelled "static". The comment shows
   `Static: 1640, Dynamic: 0`, hiding the fact that hand-written + dynamic
   attacks didn't run.

## Root Cause Analysis

### 1. main.py not found

Local check: `main.py` is at repo root, tracked in git (commit `cb08f20`),
2,345 bytes, executable bit set. Wrapper path resolution:
```python
CC_HARNESS_ROOT = Path(__file__).resolve().parents[3]  # /home/runner/work/cc-harness/cc-harness
MAIN_PY = CC_HARNESS_ROOT / "main.py"                  # /home/runner/work/cc-harness/cc-harness/main.py
```
This should work. We do not know why CI reports it missing. Possible causes:
- promptfoo invokes the wrapper from a context where `__file__` resolves differently
- A symlink or bind-mount changes the apparent path
- The CI runner uses a different checkout directory naming

### 2. redteam run ignores tests:

Documented behavior of `promptfoo redteam run`:
- Reads `redteam.plugins` for attack generation
- May not honor top-level `tests:` references in `redteam run` mode

To run hand-written + dynamic attacks, we need `promptfoo eval` mode.
To run OWASP plugin attacks, we need `promptfoo redteam run` mode.

### 3. PR comment classification

Current JS:
```js
const dynamic = all.filter(r => r.testCase?.metadata?.source === 'dynamic');
const static_ = all.filter(r => r.testCase?.metadata?.source !== 'dynamic');
```
Two categories. OWASP probes lack `source` field → all classified static.

## Goals

1. PR comment shows 3 categories: **static** (hand-written), **dynamic**
   (LLM-generated), **owasp** (cloud plugins). Failure details visible
   (probe description, pluginId, score, error message).
2. Hand-written + dynamic attacks actually run alongside OWASP.
3. main.py resolution works reliably in CI.
4. No probe fails due to "main.py not found" or repl_timeout 90s.

## Approach

### A. Split configs

Create `promptfooconfig.redteam.yaml` containing only OWASP plugin config.
Keep `promptfooconfig.security.yaml` for hand-written + dynamic attacks
(uses `promptfoo eval` mode).

Workflow runs both:
- `npx promptfoo eval -c promptfooconfig.security.yaml -o eval-results.json`
- `npx promptfoo redteam run -c promptfooconfig.redteam.yaml -o owasp-results.json`

Each produces a separate JSON. PR comment JS merges them.

### B. Wrapper path robustness

Add fallback search for `main.py` in wrapper:
1. parents[3] (current behavior)
2. parents[2]
3. Search upward from workdir up to 5 levels

If all fail, error message includes all attempted paths.

### C. PR comment: 3 categories + error details

Rewrite the JS to:
- Read TWO result files: `eval-results.json` and `owasp-results.json`
- Classify each probe:
  - **static**: `metadata.source` undefined + no `metadata.pluginId`
  - **dynamic**: `metadata.source === 'dynamic'`
  - **owasp**: `metadata.pluginId` present (any promptfoo plugin)
- For failures, show `error` message if present (truncated)
- Show probe description (with pluginId for owasp probes)

### D. Workflow: separate jobs, debug step

- Add "Debug repo state" step before redteam: print `pwd`, `ls -la main.py`, `ls eval/promptfoo/wrappers/`
- Run `eval` job + `redteam` job sequentially (redteam depends on eval)
- Bump `timeout-minutes` from 240 → 360 (closer to GitHub max, gives buffer)
- Pass both JSON files to PR comment step

## Files Changed

| Path | Change |
|---|---|
| `eval/promptfoo/promptfooconfig.security.yaml` | Remove OWASP block (move to redteam config) |
| `eval/promptfoo/promptfooconfig.redteam.yaml` | **NEW** — OWASP-only config |
| `eval/promptfoo/wrappers/cc_harness.py` | Add main.py fallback path search |
| `tests/test_cc_harness_wrapper.py` | **NEW** — test fallback path logic |
| `.github/workflows/redteam.yml` | Two jobs (eval + redteam), debug step, merged PR comment |

## Acceptance Criteria

- [ ] PR comment shows 3 columns (static / dynamic / owasp) with totals
- [ ] Hand-written attacks count > 0 in PR comment
- [ ] Dynamic attacks count > 0 in PR comment
- [ ] OWASP attacks count > 0 in PR comment
- [ ] Failed probes show error message OR descriptive label (never just "?")
- [ ] 0 probes fail with "main.py not found"
- [ ] All tests pass
- [ ] CI workflow completes within 360 minutes

## YAGNI

- ❌ Don't fix `repl_timeout 90s` separately — only 5 of 1640 probes hit it; once main.py works the success rate will be higher and timeouts mostly won't trigger
- ❌ Don't reduce OWASP plugin count — user explicitly chose to keep `owasp:llm:NN` shorthand as-is
- ❌ Don't add new security layers — out of scope for this fix

## Rollback Plan

Single commit with all 4 file changes. Revert via `git revert <sha>`.
