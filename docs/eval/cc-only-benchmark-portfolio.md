# cc-harness-only benchmark portfolio

This portfolio executes only cc-harness with `deepseek-v4-flash`. Published Claude Code numbers are
external references, not paired local evidence. Every launcher uses one logical run per task,
retains all infrastructure attempts, and resumes terminal pass/fail tasks after Ctrl+C.

## Windows CMD commands

Run from the repository root:

```cmd
scripts\run_eval_context27.cmd
scripts\run_eval_locomo.cmd --profile full
scripts\run_eval_longmemeval_context_memory.cmd
scripts\run_eval_locomo_context_memory.cmd
scripts\run_eval_memoryagentbench_context_memory.cmd
scripts\run_eval_context_memory_all.cmd
scripts\run_eval_safety8.cmd
scripts\run_eval_agentdojo.cmd
scripts\run_eval_agentdojo.cmd --profile portfolio --balanced --confirm-live
scripts\run_eval_agentharm.cmd
scripts\run_eval_terminal_bench_2_1.cmd
scripts\run_eval_swebench_verified.cmd
scripts\run_eval_final_report.cmd
```

Append `--check` for a zero-model-call prerequisite preparation and check, `--profile full` for
complete upstream scope, or `--retry-invalid` to start a new infrastructure retry generation.
The check may download pinned data, generate check fixtures or build the local wheel, but it never
starts model preflight. Rerun the identical command after Ctrl+C. Results are isolated below
`eval\result\cc-only\<benchmark>\deepseek-v4-flash\<profile>`. Check evidence uses the `check`
result directory while its manifest retains the requested `portfolio` or `full` profile.

## LoCoMo context-and-memory adaptation

`scripts\run_eval_locomo.cmd --profile full` evaluates all ten conversations and 1,986 QA through
the production context and L0-L3 memory path. It uses a stable per-sample memory scope so a frozen
ingestion snapshot remains searchable after each QA is restored into an isolated workspace.

Historical-session ingestion is stored separately from result roots. The normal command reuses a
strictly validated source-only snapshot, resumes an unpublished session build, or builds and
publishes a missing snapshot before QA. To prepare snapshots without QA:

```cmd
scripts\prepare_locomo_memory_cache.cmd --profile full
scripts\prepare_locomo_memory_cache.cmd --profile full --sample conv-26 --refresh
scripts\prepare_locomo_memory_cache.cmd --profile full --refresh-all --confirm-refresh-all
```

`--refresh-all` is the explicit ten-sample rebuild path; a failed replacement leaves the previous
valid snapshot intact. Cache state lives below `eval\cache\cc-only\locomo-memory` and is validated
before every reuse. A fresh QA run uses `--new-run`, keeps prior result evidence, and still reuses
matching snapshots:

```cmd
scripts\run_eval_locomo.cmd --profile full --new-run
```

Reports distinguish the snapshot preparation usage, warm QA usage and cold-equivalent total. A
snapshot contains historical memory state only; it never contains QA, gold answers, predictions or
grader metadata.

- A committed session or QA is never replayed after Ctrl+C. The in-flight QA alone is retried.
- A transient provider failure retries that uncommitted QA at most three times from its pre-question
  snapshot. Exhaustion is `invalid`, not a product failure; the next identical command resumes the
  same logical attempt at that QA.
- Persistent atoms are checked immediately after ingestion. Zero atoms invalidate the sample before
  QA spending begins. Every QA retains memory and context activation evidence, injected atom IDs,
  source/session provenance, relevance values, retrieval rounds and context-utilization events.
- Historical ingestion uses fact-level atoms in a benchmark-isolated `locomo:<sample>:<digest>` scope.
  Facts from different sessions are not semantically deleted; exact duplicates are removed only
  within the same session. QA runs are read-only for long-term memory, so an answer cannot change
  the evidence it is evaluated against.
- The live adaptation is one joint context-memory mode: the answer prompt receives separate
  `[working_context]` and `[long_term_memory]` channels and must use retrieved, provenance-bearing
  support. This estimates end-to-end capability; it is not a causal comparison against a
  no-memory or full-context control.
- The joint report includes one primary LoCoMo F1 plus category F1, context budget/truncation,
  historical atom/session/time coverage, supporting-evidence rate, retrieval activation,
  contract/invalid/retry counts, model calls, tokens, latency and cost. A non-empty memory
  injection without question-relevant supporting atoms is not counted as retrieval support.
- Primary scoring follows LoCoMo's category rules: partial F1 for category 1, Porter-stemmed token
  F1 for categories 2-4, and abstention accuracy for category 5. Semantic judging is diagnostic only.
- The pinned fixture's category semantics are: 1 direct fact/count/list, 2 temporal reasoning,
  3 evidence-grounded inference/commonsense, 4 cross-fact synthesis, and 5 intentionally
  unanswerable. Category-aware retrieval guidance follows these meanings; the scorer is unchanged.
- Reports separate execution, evidence validity and answer quality. LoCoMo supports a claim about
  basic context management and cross-session memory, but not compression superiority.

For a real-model protocol smoke run that is isolated from formal full results:

```cmd
scripts\run_eval_locomo.cmd --profile full --task-limit 1 --qa-limit 10
```

The ten QA are selected deterministically across all available categories; this result is diagnostic
and must not be pooled with the formal full run.

## Terminal-Bench 2.1 single-pass protocol

The active terminal evaluation is the official 89-task Terminal-Bench 2.1 dataset, pinned by
registry SHA. It is executed once per task with official Harbor task environments and verifiers.
The reportable metric is official single-pass accuracy (`reward > 0`) over the fixed denominator
of 89. This is a project evaluation, not a leaderboard submission: the official leaderboard asks
for repeated trials, so `leaderboard_compatible` remains false. Diagnostic failure causes, usage,
latency and cost are reported separately and never blended into the official score.

Run the non-mutating readiness check, then start a fresh formal evaluation:

```cmd
scripts\run_eval_terminal_bench_2_1.cmd --check
scripts\run_eval_terminal_bench_2_1.cmd --new-run --confirm-live
```

The check makes no model calls and does not start a task. The formal command prints a timestamped
`output_root` under
`eval\result\cc-only\terminal-bench-2.1\deepseek-v4-flash`. Resume that exact namespace with
`--output-root`. Completed official tasks are skipped. An interrupted current task is restarted as
the same catalog entry; no completed task is replayed. Copy the printed root into the resume command:

```cmd
scripts\run_eval_terminal_bench_2_1.cmd --output-root eval\result\cc-only\terminal-bench-2.1\deepseek-v4-flash\full-new-YYMMDDHHMMSS --confirm-live
```

The formal Harbor command does not pass timeout multipliers, resource overrides, extra Compose
files, allowed-host exceptions, verifier replacements or synthetic canaries. Official task TOML
controls the image, network permission, time budgets, verifier and reward. Each task has one
attempt; a Harbor error is retained and counted as reward zero rather than silently retried or
removed from the denominator. The runner remains serial and emits observational heartbeats, which
do not change the task environment. Keep at least 80 GB free in Docker storage.

`scripts\run_prepare_terminal_bench_2_1.cmd` remains as an alias for the same readiness check. It
does not preinstall dependencies or rewrite official task images.

The explicit legacy launcher `scripts\run_eval_terminal_bench_2_0.cmd` remains available only for
version-specific historical work. Its evidence must not be pooled with Terminal-Bench 2.1.

## Downloads and protocol adaptations

- AgentDojo installs `agentdojo==0.1.35`; that package contains benchmark suite `v1.2.2`.
- AgentHarm downloads the public-test JSON at revision
  `e23b3fe60a0da9037314b88e5ee3a0c054970dad` and sparse-checks out `inspect_evals` commit
  `b935c0e5cfa04710f016f925db75d8e81413e2cf`.
- AgentHarm uses the pinned simulated tools and grading functions. `deepseek-v4-flash` replaces the
  official GPT-4o refusal and semantic judges, so its score is a named judge adaptation.
- Terminal-Bench 2.1 uses the official `terminal-bench/terminal-bench-2-1` Harbor registry source
  pinned to `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`.
- Terminal-Bench 2.1 runs all 89 tasks once. This is the only declared deviation from the official
  leaderboard protocol, which requires at least five trials per task. The result is therefore an
  official-task single-trial evaluation, not a leaderboard submission.
- The unified context-memory domain runs LongMemEval-S Cleaned, LoCoMo and
  MemoryAgentBench with one isolated treatment run per task. Standalone NVIDIA RULER is retired; the
  `ruler_qa1`/`ruler_qa2` sources inside MemoryAgentBench remain official suite members and are not
  reported as standalone RULER.
- MemoryAgentBench requires its pinned resumable preparation download. LongMemEval-S Cleaned and
  LoCoMo use the already pinned local files when their SHA-256 matches. LongMemEval-V2 is retired from
  the active suite because the configured model cannot accept its required image inputs; historical
  V2 data and result evidence remain available for audit but are not runnable or aggregated.

`--check` never performs model preflight or task calls. Live commands require `--confirm-live`; the
Terminal-Bench launcher keeps this confirmation explicit. Safety trials are invalid unless the production safety capability reports
enabled, initialized, triggered and non-degraded activation evidence.

## AgentDojo balanced security slice

The default AgentDojo `portfolio` remains the 474-trial upstream portfolio and `full` remains the
7,786-trial scope. The balanced command now selects a frozen 500-trial extension:

```cmd
scripts\run_eval_agentdojo.cmd --profile portfolio --balanced --confirm-live
```

The 500-task catalog retains all 474 tasks from the pinned portfolio (194 benign and 280 attacked)
and adds 26 deterministic `user_task_1` attacked trials, giving 250 tasks to each safety track.
It covers all four official suites (`workspace`, `travel`, `banking`, `slack`), both `standard` and
`hardened` tracks, all pinned injection goals and all four official attacks
(`direct`, `ignore_previous`, `system_message`, `injecagent`). It is a frozen portfolio subset,
not an official complete AgentDojo score. The result root is
`eval\result\cc-only\agentdojo-v1.2.2-balanced-500\deepseek-v4-flash\portfolio`.

The prior 80-trial balanced evidence remains available without mutation. Use
`scripts\run_eval_agentdojo.cmd --profile portfolio --balanced-80 --confirm-live` only when you
explicitly need that legacy result root.

The prior `agentdojo-v1.2.2-balanced` root is retained as an infrastructure-failure
audit: its attacked trials hit a Windows path-length error before the official checker
could run and must not be interpreted as a security score.

Each trial keeps its isolated workspace, MCP configuration, launch evidence, raw model output,
pre/post environment, tool-call trace, activation manifest and official checker result. The runner
writes `manifest.json`, `catalog.json`, `state.json`, `summary.json`, `report.md` and
`integrity.json`; the report keeps benign utility, attacked utility, attack success, secure utility,
standard/hardened tracks, suite breakdown, invalid evidence, tokens, model calls, tool calls,
latency and cost separate. Run the same command after `Ctrl+C`: terminal trials are skipped, and an
interrupted trial restores its last persisted environment and tool-call checkpoint in the same
logical attempt before continuing.

### AgentDojo remediation protocol

AgentDojo now exercises the unified cc-harness security path in strict mode. MCP tools publish an
explicit capability contract (`read`, `write`, `external_write`, `network`, `credential` or
`unknown`); missing metadata is never inferred from a name. Every action is checked by the existing
`PolicyEngine` with field-level provenance. Values must come from the user request or an explicit
delegation to a source document; untrusted tool instructions cannot grant a side effect.

Successful tool results remain visible to the model only inside the existing `<untrusted>` data
container. Before egress, a deterministic source-aware detector checks meaningful quoted/proper-name
overlap. A finding invokes at most two no-tool constrained finalization attempts; raw tool text is
excluded from those retries. If the answer is still unsafe, the completed side effect is preserved
and the user receives a safe status instead of the echoed content. Policy blocks and output findings
are written to each trial's `security.json` and to the trajectory.

The official AgentDojo checker remains authoritative for `utility`, `attack_success` and
`secure_utility`. `untrusted_output_echo`, `side_effect_violation` and
`unauthorized_parameter_use` are supplementary defense telemetry and are never pooled into an
overall score. Exact development regressions can be selected with repeatable `--task-id` flags or a
JSON `--task-manifest`; the manifest uses frozen catalog task IDs and may include a reason and
baseline metrics. A remediation rerun is development evidence; after code freeze, a fresh
catalog-equivalent 500-trial run is the final holdout.

Checkpoint restoration validates the pinned AgentDojo schema and then overlays persisted mutable
fields. This is required because the pinned `Calendar` and `Inbox` models rebuild computed state
from `initial_*` fields in an `after` validator; plain `model_validate` would silently discard
events or sent mail before the official utility checker runs. The legacy 80-trial report was
offline regraded from preserved live trajectories after this fix (zero additional model calls):
63/80 trials pass, benign utility is 15/16 (93.75%), attacked utility is 48/64 (75%), attack
success is 0/64, and secure utility is 48/64 (75%). Its pre-fix report and affected checker/result
files remain under `agentdojo-v1.2.2-balanced-v2/.../portfolio/regrade-audit/pre-fix`.
