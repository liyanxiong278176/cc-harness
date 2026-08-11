# ADR 0036: Unify context and memory engineering evaluation

Status: Accepted (execution arm design superseded by ADR 0038)

Date: 2026-08-10

ADR 0038 supersedes the paired control/treatment execution design below. The benchmark catalog,
native metrics, isolation requirements and non-compensating mechanism gates remain applicable;
current commands execute treatment-only.

Context compression, offload, retrieval, long-term memory and recovery are evaluated as one
information lifecycle rather than independent scores. LongMemEval-S Cleaned, LongMemEval-V2 Small,
LoCoMo and MemoryAgentBench retain their native event semantics and benchmark-specific metrics, while
a paired control/treatment run and non-compensating mechanism gates establish whether production
cc-harness engineering caused the result. No weighted cross-benchmark score is produced.

Each benchmark, sample and arm receives an isolated runtime. Completed state is sealed as immutable
evidence and removed from the next runtime's accessible namespace; cleanup or integrity failure makes
the engineering verdict invalid. Runs are single-pass and resumable, use `deepseek-v4-flash`, label
reader or judge substitutions as adaptations, and preserve raw predictions for later rescoring.

The frozen profiles are LongMemEval-S 100/500 questions, LongMemEval-V2 Small 50/451 questions,
LoCoMo four conversations with 200 QA / ten conversations with 1,986 QA, and MemoryAgentBench 24
stratified streams with at most ten QA each / all 146 streams. Portfolio is a deterministic
development profile; only full is externally reportable. Evidence lives below
`eval/result/cc-only/context-memory/deepseek-v4-flash/<profile>/<benchmark>`.

Treatment validity requires immutable source digests, gold-field exclusion, a versioned summary with
lower post-compaction token count, complete node/ref digests, retrieval calls traceable to nodes,
isolated memory state and verified checkpoint restore. Control validity requires context, memory,
offload and retrieval to remain disabled. Fixed model-free canaries inject crashes after event, ref,
summary and checkpoint commits, then corrupt source, summary, node, ref and checkpoint evidence.
Canaries are excluded from benchmark metrics.

Preparation pins revision, size and SHA-256, resumes with HTTP Range, deduplicates by SHA-256 and
stops safely at a 50 GB managed-data soft limit. LongMemEval-V2 preparation excludes Medium. Live V2
execution must pass an actual image attachment probe or becomes unsupported without text-only
fallback.

Standalone NVIDIA RULER is removed, including its adapter, prepared data and historical result
evidence. The RULER-derived tasks that are part of the official MemoryAgentBench distribution remain
because they exercise incremental memory ingestion and retrieval inside that benchmark.
