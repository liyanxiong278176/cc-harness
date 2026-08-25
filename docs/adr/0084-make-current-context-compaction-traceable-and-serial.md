---
status: accepted
implementation: complete
---

# Make current context compaction traceable and serial

Directly revise the existing context manager—do not introduce a parallel V2 manager—so each Run has exactly one current model-context projection. Snip, Prune, and LLM Summary are mutually exclusive transforms selected at 60%, 80%, and 95% of usable input budget; immutable session events and offloaded objects remain the authority, every lossy transform writes a manifest that points directly to those originals, and Summary is generated from authoritative originals rather than earlier lossy projections. This preserves exact retrieval without duplicating raw data or allowing multiple active contexts.

## Consequences

Large tool results are offloaded immediately and represented in context by a bounded preview plus `source_ref`. At Summary time the same-Run execution path raises a serial barrier: new user inputs are durably queued but not executed until an atomically committed compaction completes; a single-writer lease and idempotent compaction identity make crash takeover deterministic. SQLite/WAL owns transactional events, queues, manifests, summaries, and the current pointer, while large immutable payloads live in the object store. The model recalls historical evidence explicitly with scoped `search_ref` followed by paginated `read_ref`; recalled text remains untrusted evidence rather than instructions.

## Implementation

The existing `ContextProjection` remains the only model-context manager. Every lossy transform is now an immutable `context_compaction_version` row, and `context_current_compaction` changes in the same SQLite/WAL transaction. Publication validates the Run-scoped writer-lease epoch and expected parent version; a committed idempotent identity is reused. JSON files under `.cc-harness/context/<run>` are compatibility and audit mirrors, not the active pointer or recovery authority.

Every Snip, Prune, and Summary row is a self-contained cumulative version. It copies forward the parent's flat effective entries, adds representations produced by the current transform, and de-duplicates by stable source reference plus representation digest. Different lossy representations of the same source remain available, while byte-identical representations are stored once. Parent links are lineage only: normal restore and recall read the current version without replaying ancestors. Legacy payloads are carried forward as one flat compatibility entry at the first v4 publication.

Durable Summary no longer rewrites the previous semantic summary. It preserves prior summary fragments exactly and asks the LLM to summarize only the newly covered authoritative range; the deterministic reducer appends and de-duplicates those fragments. Snip and Prune use the same cumulative-entry contract, so this preservation rule is not Summary-only.

Each manifest contains direct atomic source references and a scoped summary handle. The projection tells the model to call `search_ref(summary_id, query)` and then paginate exact text with `read_ref(source_ref, offset, limit)`. Context identity, scope, range, and source digests are checked before a read, and recalled content is labelled untrusted evidence. Large tool bodies—including bodies beyond the former 256 KiB limit—are published immediately as content-addressed objects; immutable node metadata is committed to SQLite while JSONL remains a compatibility mirror.

Post-transform token counts and usable-budget utilization are observations only. Snip, Prune, and Summary have no fixed completion ratio and are not rolled back merely because the resulting projection remains above a configured target; only a transform, validation, or atomic-publication failure closes the model call.

The interactive path acknowledges input only after durable FIFO insertion and drains it under the per-Run serial barrier. The default durable path retains its Run lease and durable follow-up queue; context publication adds expected-parent fencing, so a stale executor cannot publish after takeover. A compaction error fails closed before a model call and leaves accepted input queued.

## Verification

Contract tests cover exclusive thresholds, protected current input, mandatory working-state retention, cumulative Snip/Prune representations, append-only authoritative summary fragments, de-duplicated self-contained current versions, direct restore without parent replay, a three-version parent chain, stale-writer fencing, oversized-result offload, scoped search followed by exact paginated read, durable FIFO recovery, and both interactive and default durable runtime integration.
