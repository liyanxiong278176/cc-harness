# Repeatable Harness Canary

The canary is a same-model comparison between cc-harness and Claude Code. Codex is not part of the
default or supported pair. Both launch profiles request and must report `deepseek-v4-flash`.

## Frozen Inputs

`eval.canary.catalog` defines ten versioned deterministic repair tasks. Installing the catalog
writes each instruction and a byte-stable fixture ZIP into `EvalStore`, then returns immutable Task
Contracts. The catalog includes a CRLF editing regression and records protected test-file digests;
a harness cannot pass by modifying the grader inputs.

`eval.canary.advanced_catalog` adds cross-file configuration, checkpoint recovery and conflicting
decision-record tasks. Each advanced fixture is also proven to fail before execution and pass with
the reference repair before it can enter a live comparison.

## Trial Evidence

`HarnessCanaryAdapter` launches a shell-free profile in the disposable task workspace. It stores:

- structured launch evidence, raw stdout and raw stderr;
- provider-reported model identity, token usage, latency and nullable cost;
- deterministic pytest output, protected-file integrity and final implementation bytes;
- a normalized `TrialResult` whose artifacts are all content addressed.

Unknown provider cost remains `null`. Aggregate cost is published only when every selected trial
reports cost.

## Paired Retry

`PairedCanaryRunner` owns pair selection. A 429, transient 5xx, overload, connection reset or
provider retry-budget failure causes both harnesses for that task to run again after a bounded
cooldown. Earlier attempts stay in the journal. A functional pass/fail is never retried, and a
non-transient invalid result cannot be hidden by a later attempt.

Only the first round in which both harness results are non-invalid is selected for paired outcome
analysis. If no jointly valid round exists within the policy limit, the task remains invalid.

The advanced runner uses the same paired policy and immutable store. It also records executable,
profile, dependency and OS fingerprints plus source snapshots so a post-fix regression can be
compared without rewriting the original run.

## Execution Boundary

Live execution requires two preflighted `EvalRunManifest` records with identical contracts,
budgets, environment, seed, split and comparison group. Each manifest must report resolved model
`deepseek-v4-flash`. This keeps route identity evidence outside the benchmark result and prevents a
profile fallback from entering the comparison silently.
