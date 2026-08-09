# Own a unified evaluation evidence core

## Status

Accepted on 2026-08-03. Phase 1 evidence contracts, the Phase 2 durable local runner foundation,
the Phase 3 internal release-gate foundation and the Phase 4 evaluator-migration foundation are
implemented. Human calibration labeling and live migration parity evidence remain outstanding.

## Context

cc-harness already uses Promptfoo for safety evaluation and LoCoMo for memory evaluation. Their
formats and status semantics differ, while coding benchmarks and native terminal/recovery contracts
need additional runners. Making any external framework's result JSON the release source of truth
would couple release policy to that framework and make cross-suite invalid trials, vetoes and
provenance inconsistent.

Reimplementing Promptfoo, LoCoMo or public benchmark logic would discard useful specialist tooling
and create unnecessary maintenance.

## Decision

cc-harness owns a small, versioned evidence core consisting of Task Contracts, immutable Eval Run
Manifests, Trial Results, artifact references, canonical fingerprints and release status semantics.

Promptfoo, LoCoMo, Harbor/public coding benchmarks and native project contracts integrate through
adapters. Adapters preserve raw source artifacts and map them into the core; they do not redefine
release status. Reports and release decisions are rebuildable projections over core evidence.

The core remains independent of runner, database, report renderer and evaluator framework. Schema
changes require explicit versions and compatibility handling.

Every specialist trial must also prove that its target capability initialized, triggered, produced
an auditable artifact and did not silently degrade. Missing activation evidence makes the trial
invalid even when its final answer is correct. Production-path regression remains a mandatory gate
after component-isolated evaluation.

## Consequences

- Cross-suite trials share identity, provenance, invalid-run and veto semantics.
- External frameworks can upgrade or be replaced without rewriting historical release evidence.
- Adapter correctness becomes a tested boundary and raw imported evidence must remain available.
- The project must maintain schema migrations and avoid placing framework-specific fields in the
  stable core.
- Existing evaluators continue to operate until their adapters pass parity checks.
- A task outcome cannot be used as proxy evidence that Context, Memory, Loop or Safety was active.
