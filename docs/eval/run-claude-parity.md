# Running the Claude Code Parity Evaluation

The normative matrix and decision rules are in `docs/eval/claude-code-parity-matrix.md`. Run all
commands from the repository root in PowerShell unless a section explicitly names Command Prompt.
Generated bundles are written below `eval/result`.

The controlled Agent loop, Context, Memory and Tools/MCP preparation path is documented in
`docs/eval/specialist-eval-readiness.md`. Its readiness command makes no model calls and does not
download Docker images.

## Controlled specialist runs from Command Prompt

Run the four capability domains independently:

```cmd
scripts\run_specialist_agent_loop.cmd
scripts\run_specialist_context.cmd
scripts\run_specialist_memory.cmd
scripts\run_specialist_tools_mcp.cmd
```

Append `--check` for a zero-model-call preflight. Each command has a separate fixed directory below
`eval\result`, state file, raw evidence tree, bundle and domain report. Press `Ctrl+C` to terminate
the active child and rerun that same domain command to continue. Completed harness sides are skipped;
the unfinished side receives a new retained attempt. The four runs contain 24, 27, 34 and 32 pairs,
one pass per task, so they are diagnostic rather than release-level statistical claims. See
`docs/eval/specialist-eval-readiness.md` for the matrix, output contract and watchdog boundary.
The current frozen contract is v5; completed v4 evidence remains in its v4 directories and is never
resumed or overwritten by these commands.

## Prerequisites

- The project `.env` configures the cc-harness OpenAI-compatible route and Claude Code's
  Anthropic-compatible route to the same `deepseek-v4-flash` service.
- `claude --version` resolves to the pinned default, currently `2.1.221`.
- `~/.claude/settings.json` contains the intended isolated Claude Code settings.
- The required sandbox and test dependencies are available before a live run.

The runner performs model and product-version preflight checks before comparison tasks. A mismatch
stops the run instead of producing opponent failures.

## Live Smoke

```powershell
uv run python scripts\run_claude_parity.py `
  --suite smoke `
  --confirm-live
```

Smoke runs one advanced deterministic task once. It validates plumbing and can report regressions,
but it always withholds positive `parity` or `exceeds` claims.

## Paired Regression

```powershell
uv run python scripts\run_claude_parity.py `
  --suite regression `
  --repetitions 3 `
  --maximum-attempts 3 `
  --cooldown-seconds 30 `
  --confirm-live
```

The runner persists an AB/BA-balanced schedule before execution. A provider retry creates a new
attempt inside the same repetition and is not counted as another statistical sample.

Canary contract version `1.1.0` gives standard tasks a 180-second total wall-time budget and a
200,000-token input budget. The checkpoint-recovery task has a 300-second total wall-time budget.
These limits include a 30-second deterministic-grader reserve and are identical for both harnesses.

Use repeated `--task` arguments for a controlled subset:

```powershell
uv run python scripts\run_claude_parity.py `
  --suite regression `
  --task canary.advanced.cross-file-runtime-config `
  --task canary.advanced.checkpoint-recovery `
  --repetitions 3 `
  --confirm-live
```

A subset that does not meet the suite's coverage or sample requirements is rejected or reported as
`inconclusive`; it cannot become a release claim.

## Observational Unbounded Run

Use the observational profile to measure each harness under its own default loop behavior without
eval-injected model-call, token, tool-call, cost or normal wall-time limits:

```powershell
uv run python scripts\run_claude_parity.py `
  --suite regression `
  --repetitions 3 `
  --observe-unbounded `
  --emergency-watchdog-seconds 3600 `
  --confirm-live
```

The emergency watchdog is infrastructure protection for a genuinely stuck process. It is recorded
separately and is not a normal task budget. Observational contracts use version
`1.2.0-observe`; their evidence must not be pooled with bounded `1.1.0` results. These runs reveal
natural completion time and resource use, but bounded runs remain the release and production-SLO
gate.

## Imported Evidence

SWE-bench, Harbor, LoCoMo, Promptfoo, human-interaction and operational-conformance jobs produce a
normalized `eval.normalized-pair-bundle.v1` document. Analyze one or more bundles without making
model calls:

```powershell
uv run python scripts\run_claude_parity.py `
  --suite release `
  --import-evidence D:\evidence\swebench\bundle.json `
  --import-evidence D:\evidence\harbor\bundle.json `
  --import-evidence D:\evidence\locomo\bundle.json `
  --import-evidence D:\evidence\safety\bundle.json `
  --import-evidence D:\evidence\human\bundle.json `
  --import-evidence D:\evidence\operational\bundle.json
```

Import paths for trajectories, patches and grader evidence are relative to each source bundle. The
importer rejects traversal, missing artifacts, unknown sources, domain overclaiming, model drift and
Claude Code version drift.

### Harbor agent integration

For live SWE-bench comparison, the resumable paired runner is the recommended entry point. It
persists the randomized AB/BA schedule before the first model call and executes both harnesses
serially against the same pinned task revision:

#### Full SWE-bench Verified 500 run from Command Prompt

The formal 500-task coding run has a dedicated frozen launcher for Windows `cmd.exe`. Check every
input without making model calls:

```cmd
scripts\run_harbor_verified500.cmd --check
```

Start the live comparison with one command from the repository root:

```cmd
scripts\run_harbor_verified500.cmd
```

The fixed output root is
`eval\result\harbor-verified500-deepseek-v4-flash`. Press `Ctrl+C` to interrupt, then run the same
command again. Every trial already selected in `state.json` is skipped. A partially written,
unrecorded current attempt is retained as `attempt-N-interrupted-<timestamp>-<id>` and only that
current harness trial is rerun. The runner never replaces or archives the whole result directory.
Preflight also requires a responsive Docker daemon. A Harbor launcher exit that occurs before any
auditable job is created is retained under `launcher_failures` and does not consume the task's
formal retry allowance; after fixing Docker or another local runtime problem, rerun the same CMD
command.

This run freezes all 500 unique tasks from Harbor `0.20.0` dataset
`swe-bench/swe-bench-verified@sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`.
The catalog is `eval\harbor\catalogs\swebench_verified_500.json`; its task-list digest is
`sha256:c4657a3129950aac592c70ea3fce04f4fba2ac384855265e79368a1e5723499f` and its file digest is
`sha256:7755d14c804a9c25ea9fdb34467cd994e7090f586349f248b62b952416030268`.
The dedicated cc-harness wheel digest is
`sha256:898398dcbc527c84e99610b0c393c509473e33bf7961af46ae56cfb0df77d7e6`.

The run contains 500 pairs and 1,000 serial harness trials, with one repetition per task. It is a
complete SWE-bench Verified coding comparison, but it does not replace the controlled context,
memory, tools/MCP, agent-loop, or safety suites. One repetition also does not estimate within-task
stochastic variance.

During execution, inspect `state.json`, `schedule.json`, `progress.log`, and `raw`. After all trials
finish, the runner writes `normalized/bundle.json` and the complete `analysis` directory, including
`summary.json`, `parity-report.md`, and `integrity.json`. `frozen-inputs` retains the exact catalog
and wheel; `raw` retains Harbor results, trajectories, patches, graders, exceptions, and all retry
attempts.

#### Custom development subsets

```powershell
uv run python scripts\run_harbor_parity.py `
  --output-root eval\result\harbor-dev `
  --task swe-bench/matplotlib__matplotlib-14623 `
  --task swe-bench/astropy__astropy-12907 `
  --repetitions 1 `
  --random-seed 20260806 `
  --maximum-attempts 2 `
  --cooldown-seconds 30 `
  --suite dev `
  --confirm-live
```

The Harbor profile does not impose eval-level model-call, tool-call, token or cost budgets. The
cc-harness plugin also enables unbounded agent iterations. The task environment's native timeout
remains emergency infrastructure protection and is recorded as invalid evidence rather than a
competitive failure. Do not pool these observations with bounded canary results.

Rerun the exact same command to resume. Completed trials are skipped from `state.json`; transient
provider failures keep their original attempt and may be retried, while product errors, parser
errors and task timeouts are retained as invalid without retry. Changing the task list, seed,
attempt policy, model inputs or frozen artifacts makes resume fail closed.

Resume replays the selected raw Harbor evidence before deciding that a job is complete. Agent setup
timeouts and setup-command failures whose retained `trial.log` or `exception.txt` contains a network
5xx, rate-limit or connection marker may append the next attempt up to `--maximum-attempts`.
Successful trials and verifier failures are not rerun. When a selected retry changes, the runner
atomically rebuilds `normalized` and `analysis`, archives their previous complete versions as
`*-superseded-<timestamp>-<id>`, and preserves every raw attempt. The exported AB/BA order comes
from the frozen schedule rather than retry timestamps.

The runner pins the SWE-bench Verified dataset by SHA-256, copies the selected cc-harness wheel to
`frozen-inputs`, and records SHA-256 digests for the source wheel, `.env`, and isolated Claude
settings. Secrets are not copied into the result. Live progress is appended to `progress.log`; raw
Harbor jobs, trajectories and graders remain below `raw`. On completion, the normalized pair bundle
is written to `normalized/bundle.json` and the domain analysis to `analysis`.

Normalized export is staged and published atomically. If export fails, a subsequent run archives the
partial output as `normalized-failed-<timestamp>-<id>` and rebuilds from the selected raw jobs. It
does not rerun completed trials or discard the failed export evidence.

Claude Code's final `modelUsage` record is the billing source of truth because its Harbor trajectory
can omit child calls. Trajectory records still determine model/tool step counts. Claude WebSearch
usage is normalized separately at `$0.01` per request. When Harbor cannot derive its ATIF
`trajectory.json` but the original Claude stream is complete, the exporter retains
`claude-code.txt` and reconstructs model/tool counts from unique message and tool-use IDs. Missing
both forms remains a hard export error.

The sections below document the lower-level manual workflow used for plugin diagnosis and offline
export. Manual runs are not formal comparison evidence unless their execution order is balanced.

Build the exact cc-harness wheel before a Harbor job:

```powershell
uv build --wheel --out-dir eval\result\harbor-wheel
```

Harbor can then load the local installed-agent plugin. The plugin uploads the wheel into each task
container, runs cc-harness with explicit container-local host execution, records its JSON trajectory
and maps cache-inclusive usage into Harbor's `AgentContext`:

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:PYTHONUTF8 = '1'
$wheel = (Get-ChildItem eval\result\harbor-wheel\*.whl | Select-Object -First 1).FullName

uvx --from harbor==0.20.0 harbor run `
  --dataset swe-bench/swe-bench-verified `
  --agent harbor_plugins.cc_harness_agent:CCHarnessHarborAgent `
  --agent-kwarg "wheel_path=$wheel" `
  --model deepseek-v4-flash `
  --env-file .env `
  --n-tasks 1 `
  --n-concurrent 1 `
  --jobs-dir eval\result\harbor-jobs-cc `
  --yes
```

Claude Code's Anthropic-compatible gateway is configured in the isolated user settings rather than
the project `.env`. Forward only those two route values into Harbor, pin the product version, and run
the identical task selection:

```powershell
$claudeSettings = Get-Content "$HOME\.claude\settings.json" -Raw | ConvertFrom-Json
$env:ANTHROPIC_AUTH_TOKEN = [string]$claudeSettings.env.ANTHROPIC_AUTH_TOKEN
$env:ANTHROPIC_BASE_URL = [string]$claudeSettings.env.ANTHROPIC_BASE_URL

uvx --from harbor==0.20.0 harbor run `
  --dataset swe-bench/swe-bench-verified `
  --agent claude-code `
  --agent-kwarg version=2.1.221 `
  --model deepseek-v4-flash `
  --env-file .env `
  --n-tasks 1 `
  --n-concurrent 1 `
  --jobs-dir eval\result\harbor-jobs-claude `
  --yes
```

Use exact timestamped Harbor job directories when exporting. The exporter rejects task checksum,
environment, model and Harbor-version mismatches; excludes setup/install time; reconstructs model
and tool calls from trajectories; applies the frozen normalized tariff; marks framework exceptions
as invalid; and retains both trajectories and grader results:

```powershell
uv run python scripts\export_harbor_pair.py `
  --candidate-job eval\result\harbor-jobs-cc\<timestamp> `
  --baseline-job eval\result\harbor-jobs-claude\<timestamp> `
  --output-dir eval\result\harbor-pair-<timestamp>
```

Formal runs must alternate AB/BA order across repetitions. A run where every cc-harness trial occurs
before every Claude Code trial is pipeline evidence only and must not be used for comparative claims.

## Result Bundle

Each command prints the exact result paths and digests. The default layout is:

```text
eval/result/parity-<timestamp>/
  manifest.json
  schedule.json
  trials/
  trajectories/
  patches/
  scoring/
  summary.json
  parity-report.md
  integrity.json
```

`summary.json` is the machine-readable decision. `parity-report.md` is its human projection.
`integrity.json` contains SHA-256 digests for all decision files materialized in the bundle.

Token efficiency uses cache-inclusive logical input. For Claude Code this is
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`; total tokens then add
`output_tokens`. Cache categories are also retained separately so logical volume and provider cache
behavior can be analyzed independently. Cost is comparable only when the evidence freezes the
billing source or model tariff used by both harnesses. A harness-reported amount without equivalent
provenance on the other side is retained for diagnosis but cannot produce a cost ratio; unavailable
cost remains `null`.

Live parity runs freeze pricing contract `deepseek-v4-flash.claude-code-route` version `1.0.0` in
`manifest.json`. It normalizes both harnesses at `$5/M` uncached input, `$6.25/M` cache creation,
`$0.5/M` cache read and `$25/M` output. Claude Code's own reported amount is retained separately and
must agree with the normalized amount within one micro-USD; a mismatch invalidates the trial as
pricing drift.

If a parser correction is needed after a run, preserve the original bundle and append a verified
offline audit:

```powershell
uv run python scripts\audit_parity_telemetry.py `
  eval\result\parity-<timestamp>
```

This verifies each trajectory against the digest recorded in `summary.json`, then writes
`telemetry-audit.json` and `telemetry-report.md`. It does not rewrite the original summary, report or
integrity manifest.
