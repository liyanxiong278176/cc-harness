# ADR 0023: Use layered local long-term memory

Status: Accepted

Date: 2026-08-08

## Context

The current memory hooks are only partly wired, short sessions often produce no durable memory,
and runtime enablement disagrees with configuration. A flat collection of generated summaries also
cannot reliably preserve source evidence, conflicting updates or the boundary between projects.

## Decision

Use TencentDB Agent Memory's public L0-L3 model as the conceptual baseline while retaining a local,
Python-native implementation. Every completed turn is captured as immutable L0 Conversation
evidence. An asynchronous, restartable pipeline extracts L1 Atoms, groups them into L2 project
Scenarios and derives conservative L3 Core knowledge. Exit, checkpoint and compaction boundaries
must durably capture pending L0 evidence but do not block on higher-layer extraction.

Recall combines lexical and vector retrieval with rank fusion, then applies project scope,
provenance, validity, authority and context-budget rules. Higher layers provide navigation and
stable context; the runtime can drill down to L1 or L0 to verify a claim. Conflicts supersede rather
than overwrite prior records, and forgetting removes derived retrieval state while retaining only
the separately governed original event history.

The project does not adopt the full Memory Hub, Gateway, TCVDB, COS and Redis deployment. The
single memory enable switch controls capture, extraction and recall, and embedding configuration is
propagated through the shared runtime. Ordinary coding may visibly degrade when auxiliary memory is
unavailable; a Memory specialist trial becomes invalid.

## Consequences

- Memory remains usable offline and isolated per local project without adding a service stack.
- L0 capture and L1-L3 convergence have separate health and latency metrics.
- Schema versions, tombstones and extraction checkpoints become durable compatibility surfaces.
- TencentDB Agent Memory is a design reference, not a runtime dependency or claim of behavioral
  equivalence.
