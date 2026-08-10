# Context-Memory Engineering Evaluation

This suite evaluates one information lifecycle in cc-harness: immutable native events, context
projection, versioned compaction, symbolic offload, retrieval, long-term memory, conflict update and
session recovery. It does not run Claude Code and does not calculate a weighted score across
benchmarks.

## Frozen profiles

| Benchmark | `portfolio` | `full` |
| --- | ---: | ---: |
| LongMemEval-S Cleaned | 100 questions | 500 questions |
| LongMemEval-V2 Small | 50 questions | 451 questions |
| LoCoMo | 4 conversations, 200 QA | 10 conversations, 1,986 QA |
| MemoryAgentBench | 24 streams, at most 10 QA each | 146 streams, all QA |

The portfolio is a deterministic development subset. Only complete, untrimmed `full` evidence may
be presented as an external benchmark result. DeepSeek reader or judge substitutions are adaptations,
not official same-condition leaderboard scores.

## Prepare and check

LongMemEval-S and LoCoMo are already local and are accepted only when their pinned size and SHA-256
match. Prepare the other datasets before a live run:

```cmd
scripts\run_eval_longmemeval_v2_context_memory.cmd --prepare-only
scripts\run_eval_memoryagentbench_context_memory.cmd --prepare-only
```

The preparer pins the Hugging Face revision, records every size and SHA-256, resumes with HTTP Range,
and publishes downloads through a content-addressed object store. It downloads only LongMemEval-V2
Small. Dataset plus context-memory evidence has a 50 GB soft limit; crossing it stops safely and
retains `.part` state for the same command to resume.

Run zero-model checks in this order:

```cmd
scripts\run_eval_longmemeval_context_memory.cmd --check
scripts\run_eval_longmemeval_v2_context_memory.cmd --check
scripts\run_eval_locomo_context_memory.cmd --check
scripts\run_eval_memoryagentbench_context_memory.cmd --check
scripts\run_eval_context_memory_all.cmd --check
```

Every check runs the fixed recovery/tamper canaries but performs no model call. LongMemEval-V2's real
image capability probe runs immediately before live execution. If it fails, the run is
`unsupported`; the evaluator never silently emits a text-only full result.

## Live execution and resume

Run the same four commands, in the same order, without `--check`. Append `--profile full` for the
complete scope. Each CMD supplies the explicit live confirmation. Ctrl+C leaves the current phase
and logical `attempt-1` resumable; rerun the identical CMD. A completed arm is skipped only after its
recorded result digest verifies. Active runtime state is sealed after the arm and cannot be reopened
by a later task.

Live evidence is written below:

```text
eval\result\cc-only\context-memory\deepseek-v4-flash\<profile>\<benchmark>
```

Check evidence is isolated below:

```text
eval\result\cc-only\context-memory\deepseek-v4-flash\check\<profile>\<benchmark>
```

The aggregate output is the sibling `aggregate` directory. It retains each benchmark's metric and
mechanism verdict; `overall_score` and `cross_benchmark_weighted_score` are always null.

To make the engineering gate non-optional, treatment materializes every native event and ingests it
through the production `Read` tool with an evaluation-only one-token offload threshold. This forces
real node/ref creation and a source-traceable `search_ref`/`read_ref` call before each answer. The
threshold override is recorded as a protocol adaptation in every manifest and report.

MemoryAgentBench dispatches grading by official source: AR/CR use normalized
`substring_exact_match`, TTL and DetectiveQA use `exact_match`, and results are aggregated at QA
level by capability and source. LongMemEval and InfBench retain their judge-based shapes with
DeepSeek explicitly labeled as the adapted judge; Recsys uses adapted normalized-label Recall@5
because the pinned Hugging Face distribution does not include the official `entity2id` mapping.

## Remaining limitations

- Live benchmark calls are intentionally not part of repository tests and can be costly.
- MemoryAgentBench preparation requires the `benchmarks` optional dependency for Parquet (`pyarrow`).
- LongMemEval-V2 Small needs about 7.1 GB of downloads plus extracted screenshots and retained
  evidence; available disk must remain below the 50 GB managed-data soft limit.
- Auxiliary memory extraction/decider usage follows the existing runtime accounting caveat and may
  undercount formal Memory cost until `TurnTokenStats` includes every auxiliary call.
- MemoryAgentBench's official `ruler_qa1` and `ruler_qa2` sources are retained inside that suite and
  are never reported as standalone NVIDIA RULER results.
