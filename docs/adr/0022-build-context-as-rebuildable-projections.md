# ADR 0022: Build context as rebuildable projections

Status: Accepted

Date: 2026-08-08

## Context

The current compactor mutates model messages in place, persisted sessions overwrite prior message
rows, and project-global offload references cannot prove which source material produced a summary.
This saves tokens but makes recovery, conflict diagnosis and specialist evaluation unreliable.

## Decision

Immutable session events and content-addressed tool artifacts are the source of truth. Before each
model call, cc-harness builds a temporary Context Projection using the existing Snip, Prune and
Summary tiers. Summaries are versioned and retain source event and artifact references; projection
updates never delete or replace the originals.

Large results are offloaded into session-isolated symbolic nodes and a versioned task graph. The
model receives bounded summaries and may progressively retrieve evidence through paginated
`ReadRef`, `SearchRef` and `InspectNode` operations. Context pressure comes from the verified Model
Capability Profile, with an explicit recorded override when necessary; an evaluation with unknown
window identity is invalid.

## Consequences

- Sessions, summaries and node graphs can be rebuilt and audited after a crash or policy change.
- Storage usage grows until reachability-based retention removes unreferenced artifacts.
- Projection generation becomes a critical pre-model boundary and fails closed when it cannot
  protect the actual context window.
- Existing mutable sessions require a provenance-preserving import path rather than in-place
  conversion.
