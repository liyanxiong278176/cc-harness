# ADR 0040: Resume context-memory trials from committed checkpoints

Status: Accepted

Date: 2026-08-13

Context-memory benchmark trials can spend substantial model budget ingesting a conversation before
answering its questions. Replaying every historical session after a user interruption wastes that
budget and can introduce duplicate memory facts. A partially written child launch is not enough to
prove that its side effects and result are complete.

Decision

The LoCoMo treatment adapter persists a committed checkpoint after each successful ingestion
launch by copying the isolated workspace and model home, then publishing a completion marker. It
also treats a QA result as committed only when its result JSON and valid launch evidence are both
present. On resume, the runner reuses the interrupted attempt directory, restores the highest
contiguous ingestion checkpoint, skips its completed sessions, and skips the highest contiguous QA
prefix. The child launch that was active at interruption is retried; its uncertain side effects are
discarded by restoring the previous checkpoint or query snapshot first.

The checkpoint is valid only for the existing immutable run contract and sample input. It is not a
cross-run cache and is never shared between samples. A completed trial still writes the ordinary
terminal result and summary, so checkpoint files are execution evidence rather than score inputs.

Consequences

- Ctrl+C followed by the same command no longer re-ingests completed LoCoMo sessions or re-asks
  completed questions.
- A single in-flight model call may be repeated, which is the safe boundary when its durable side
  effects cannot be proven.
- Per-item snapshots consume disk space, but keep recovery isolated and auditable.
- Changing the immutable contract still rejects the result root instead of mixing evidence.
- A checkpoint-preserving invalid LoCoMo result is retained under the same attempt before that
  attempt is reopened; its completed ingestion and QA prefix remain authoritative.
