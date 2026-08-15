# ADR 0041: Reuse validated LoCoMo pre-ingestion snapshots across runs

Status: Accepted

Date: 2026-08-14

LoCoMo historical-session ingestion is expensive and contains no QA answers, so the evaluator
publishes a source-only snapshot per sample after complete ingestion and evidence validation. A
later smoke, portfolio or full run may restore that snapshot only when its content-addressed
identity matches the sample, model, ingestion contract, memory/context implementation, capability
profile and protocol; result roots remain separate and QA state is never cached. Missing or stale
snapshots are built with per-session checkpoints, duplicate live builders are rejected, and a
published snapshot is never replaced until its replacement passes validation. Reports expose both
the snapshot preparation cost and the cold-equivalent total, while `--new-run` starts fresh QA
against retained snapshots.
