# ADR 0038: Use treatment-only context-memory runs

Status: Accepted

Date: 2026-08-11

The unified context-memory commands now execute one isolated treatment run per benchmark task.
The former control arm is removed from the context-memory contract, state, normalized results,
mechanism gates and native metric summaries. This keeps every run focused on the production
context, offload, retrieval and long-term-memory lifecycle without performing a second recency-only
execution.

The manifest and state schemas are versioned as treatment-only contracts. Existing paired result
roots remain immutable historical evidence and cannot be resumed by the new runner. A fresh result
root is required for treatment-only execution. Each completed task still records raw evidence,
sealed runtime state, mechanism-gate results, an integrity manifest and the automatic benchmark
report.

The three active benchmark commands and the aggregate command retain their independent native metrics;
the aggregate never calculates a cross-benchmark score. The `context-memory-control` capability
profile remains available only for model-free preflight and isolated judge calls; it is not a
benchmark arm.
