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

## Downloads and protocol adaptations

- AgentDojo installs `agentdojo==0.1.35`; that package contains benchmark suite `v1.2.2`.
- AgentHarm downloads the public-test JSON at revision
  `e23b3fe60a0da9037314b88e5ee3a0c054970dad` and sparse-checks out `inspect_evals` commit
  `b935c0e5cfa04710f016f925db75d8e81413e2cf`.
- AgentHarm uses the pinned simulated tools and grading functions. `deepseek-v4-flash` replaces the
  official GPT-4o refusal and semantic judges, so its score is a named judge adaptation.
- Terminal-Bench's available Harbor registry source is `terminal-bench@2.0`; the historical CMD and
  result slug retain `2.1` but the manifest discloses the actual source.
- The unified context-memory domain runs LongMemEval-S Cleaned, LoCoMo and
  MemoryAgentBench with one isolated treatment run per task. Standalone NVIDIA RULER is retired; the
  `ruler_qa1`/`ruler_qa2` sources inside MemoryAgentBench remain official suite members and are not
  reported as standalone RULER.
- MemoryAgentBench requires its pinned resumable preparation download. LongMemEval-S Cleaned and
  LoCoMo use the already pinned local files when their SHA-256 matches. LongMemEval-V2 is retired from
  the active suite because the configured model cannot accept its required image inputs; historical
  V2 data and result evidence remain available for audit but are not runnable or aggregated.

`--check` never performs model preflight or task calls. Live commands require `--confirm-live`; the
CMD launchers supply it. Safety trials are invalid unless the production safety capability reports
enabled, initialized, triggered and non-degraded activation evidence.
