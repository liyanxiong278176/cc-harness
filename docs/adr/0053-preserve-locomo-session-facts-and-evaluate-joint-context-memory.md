# ADR 0053: Preserve LoCoMo session facts and evaluate joint context-memory behavior

Status: Accepted

Date: 2026-08-14

The LoCoMo adapter stores historical sessions as independently retrievable fact atoms in a
sample-scoped namespace. Each atom carries the source session, timestamp when available, fact
index and benchmark provenance. Exact duplicate text is idempotent within one session; facts
from different sessions are retained even when the generic product memory conflict detector
would consider them semantically similar. This preserves temporal changes and prevents the
benchmark's 19 sessions from collapsing into one surviving atom.

During LoCoMo question answering, the memory capability is read-only. The QA process receives
separate working-context and long-term-memory channels, and its prompt can issue recall but not
save, supersede or delete operations. Supporting evidence is computed from retrieved atom IDs,
provenance and question overlap; merely injecting an unrelated atom is not evidence. Answers
must abstain when neither channel provides support, while temporal conflicts are resolved using
the explicit source/time anchors.

This is intentionally one joint evaluation mode rather than three live arms. It measures the
production end-to-end context-memory path and reports diagnostics for context utilization,
retrieval support, historical coverage and execution validity. Without a paired full-context and
no-memory control, the primary score must not be described as a causal estimate of memory's
incremental gain. A future controlled study may add those arms without changing the joint-mode
contract.

The ingestion snapshot and QA checkpoints include the adapter protocol version. Upgrading the
fact-preservation or answer contract therefore invalidates stale records while preserving valid
same-protocol checkpoints for interruption-safe resume.
