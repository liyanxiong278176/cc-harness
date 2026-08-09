# Eval v3: Release-Grade Harness Evaluation

## Status

Accepted for implementation on 2026-08-03.

## Objective

Build an evaluation system that can make reproducible release decisions and support a fair
comparison with Claude Code. Diagnostic scores remain useful, but they do not replace release
evidence. The normative scope, matrix, fairness rules and statistical thresholds are defined by
`docs/eval/claude-code-parity-matrix.md`.

The design preserves the existing Promptfoo safety suite, LoCoMo memory evaluation, trajectory
capture, calibration work and confidence intervals. Those systems become adapters around a
project-owned evidence contract instead of separate sources of release truth.

## Decision model

Evaluation has two products:

1. A release gate answers whether a candidate may ship. Safety, privacy, data integrity, core
   correctness, recovery and platform-contract failures are vetoes and cannot be averaged away.
2. A diagnostic suite locates capability boundaries and generates hypotheses. A diagnostic becomes
   a gate only after its task, grader and variance are controlled.

Outcomes and observable state have the highest grading authority. LLM judges are used only where
deterministic grading is insufficient, with a fixed independent judge, human calibration and equal
treatment of every compared agent. Hidden chain-of-thought is neither collected nor scored.

## Architecture

```text
versioned Task Contracts       immutable Eval Run Manifest
           |                              |
           +---------------+--------------+
                           |
                    durable trial runner
                           |
        +------------------+-------------------+
        |                  |                   |
   native adapter     Promptfoo/LoCoMo    Harbor/public adapters
        |                  |                   |
        +------------------+-------------------+
                           |
             append-only Trial Results + artifacts
                           |
                   deterministic gate engine
                           |
              release evidence bundle / reports
```

The stable core owns identity, status, provenance and content fingerprints. Adapters may execute a
task or import evidence, but cannot invent another meaning for `pass`, `fail`, `invalid`, a veto, or
an artifact. Reports are projections and can always be rebuilt from manifests, trial results and
content-addressed evidence.

## Core contracts

### Task Contract

A Task Contract freezes task identity and version, suite version, state profile, instructions,
initial state, resource budget, capability domains and graders. Instructions and initial state are
content-addressed artifacts, so private holdout material can stay outside Git while remaining
identifiable. Graders identify their implementation and version; deterministic graders have
priority over LLM and human graders.

Changing an instruction, fixture, rubric, threshold or grader version creates a different contract
fingerprint. Results from different fingerprints are not pooled silently.

### Eval Run Manifest

The manifest records the experiment before execution: candidate source revision, executable and
harness-profile fingerprints, requested and server-resolved model version, judge configuration,
task contract fingerprints, frozen split, environment, dependency fingerprint, network mode,
resource budget, random seed and orchestration version.

Formal manifests are immutable. Corrections are future append-only events that identify the
superseded statement; they never rewrite a completed run.

### Trial Result

A trial is one attempt by one subject on one Task Contract. Its only terminal statuses are:

- `pass`: valid evidence proves the required outcome.
- `fail`: the trial was valid and did not satisfy the required outcome.
- `invalid`: infrastructure, evidence or contract failure prevents a capability conclusion.

`inconclusive` exists only at aggregate comparison level. It is not a fourth trial outcome.
Successful and failed trials require an outcome artifact. Invalid trials require an explicit
reason. Every result references its run and task fingerprints and may reference the observable
trajectory, logs, diffs and grader details.

### Canonical serialization

All core records use strict Pydantic v2 models with forbidden unknown fields and frozen instances.
Canonical UTF-8 JSON uses sorted keys, compact separators, explicit nulls and finite numbers. The
content identity is `sha256:<lowercase hex>` over those bytes. Schema versions are explicit and
fingerprints do not depend on filenames or storage locations.

## Capability matrix

Every task maps to at least one of nine stable domains:

1. Coding Outcome
2. Agent Loop
3. Context Management
4. Memory
5. Tools & Protocols
6. Safety & Privacy
7. Reliability & Recovery
8. Human Interaction
9. Operational Fitness

Reports show domain evidence and vetoes separately. There is no compensating global score.

## Profiles and fairness

State profiles isolate clean coding, warm coding, context, memory, recovery and security behavior.
Compared agents receive the same task state, budget, network policy and model. The initial target
model is `deepseek-v4-flash`; the manifest records the actual server-resolved version. Harness
benefit claims use paired ablations in which the target harness component is the primary changed
variable.

Sampling is paired, risk-stratified and adaptive. Minimum samples are set by failure consequence;
trials continue until the configured uncertainty target is reached or the budget is exhausted. A
comparison that exhausts its budget without adequate precision is `inconclusive`.

## Isolation and evidence

Every formal trial runs in a disposable environment. A trial preserves observable input/output,
tool calls and results, permission decisions, filesystem state changes, retry/cancel/recovery
events, resource use and final outcome. Secrets and unrelated user data are excluded or redacted
before storage. Raw evidence remains local unless the user explicitly enables publication.

L4 emits a content-addressed evidence bundle containing the machine-readable decision, manifest,
nine-domain matrix, paired comparisons, vetoes, regressions, invalid trials, expiring exceptions
and raw artifact references. Markdown and HTML are rebuilt views of that bundle.

## Evaluation cadence

- L0: focused local checks and ordinary CI.
- L1: PR regression and contract checks.
- L2: nightly broader regression, reliability and safety runs.
- L3: weekly public capability and paired ablation runs.
- L4: release qualification using frozen internal gates and complete evidence bundles.

Private holdout tasks are never exposed to the development loop. Exposure retires a task into the
regression set and triggers holdout rotation.

## Migration

1. Introduce the evidence contracts and fingerprinting without changing existing evaluators.
2. Add durable local execution with fail-closed subprocess handling and resumable trial attempts.
3. Implement internal Task Contracts and release veto logic, including TUI scrolling, resize,
   input, cancellation and terminal-restoration contracts.
4. Wrap Promptfoo and LoCoMo while retaining their raw artifacts and existing reports.
5. Add Harbor, SWE-bench Verified and parity adapters under identical model and budget profiles.
6. Add production-failure intake, holdout governance, CI shards and remote workers only when local
   throughput measurements justify them.

## Acceptance for Phase 1

- Unknown fields and malformed identifiers, digests, versions, timestamps and result combinations
  fail validation.
- Core records are immutable and use immutable collection types.
- Canonical serialization and SHA-256 fingerprints are deterministic across round trips.
- Task, run and trial identities cannot be confused across adapter boundaries.
- Adapter protocol code depends only on the core contracts.
- Focused pytest and Ruff checks pass.

## Phase 2 implementation notes

The local runner persists runs, queued trials, leased attempts, heartbeats, cancellation requests,
terminal results and append-only lifecycle events in SQLite WAL. Manifests, trial results and raw
artifacts are written content-first to a SHA-256 object store. A stale worker lease becomes
`outcome_unknown`; retry is explicit and creates a new attempt linked to its parent.

Each adapter receives a freshly materialized workspace and an artifact repository. ZIP/TAR input
is checked for destination escape and unsafe members, and the workspace is deleted on every exit
path. Adapter exceptions, timeouts, missing/corrupt artifacts, identity mismatches and reported
resource use above the Task Contract budget all fail closed as `invalid` evidence.

## Phase 4 implementation notes

Promptfoo and LoCoMo remain specialist evaluators, but their output is no longer a release-policy
input by itself. Their import adapters validate frozen instruction schemas, reject workspace path
escape and malformed results, preserve raw framework artifacts by digest, and emit ordinary Trial
Results. Promptfoo contributes safety/privacy, tools/protocols and agent-loop evidence. LoCoMo
contributes memory, context-management and reliability/recovery evidence.

LoCoMo live quality judging is explicit rather than ambient: offline calls do not instantiate
Deepeval merely because local credentials exist. Its unified runner emits metrics JSON alongside
the existing JSON and HTML views. Promptfoo command failures propagate and the weekly bounded
red-team workflow retains generated attacks and raw result JSON.

Placeholder calibration labels are excluded from the gold baseline. A baseline record is valid
only with two distinct annotator identities, two binary labels, a third adjudicator identity, an
adjudicated label and a timestamp. The existing 50 records remain candidates until that human work
is performed.

## Phase 3 implementation notes

The internal regression catalog defines one deterministic contract for each of the nine capability
domains. Contracts reference explicit pytest targets and a frozen source archive; the native
adapter never accepts a shell string, exposes only a small environment allowlist, captures bounded
stdout/stderr artifacts and treats incomplete output as invalid evidence.

The gate engine validates manifest, contract, trial and grader identities before deciding. Veto
failures block immediately. Missing, duplicated, extra or invalid evidence cannot produce a pass;
L4 treats these conditions and missing capability domains as vetoes. Lower tiers report
`inconclusive` when evidence is insufficient. Paired harness comparisons report a discordant-pair
Wilson interval for diagnosis, but final parity claims use task-clustered success-rate differences,
non-inferiority and efficiency intervals from `docs/eval/claude-code-parity-matrix.md`.

Release decisions are immutable machine records stored in the content-addressed evidence store and
referenced by an append-only lifecycle event. Markdown is a projection of that record and never
recalculates status.

## Sources

- Chapter 6, `AI-Agents-in-Depth-zh-CN(3).pdf`
- Anthropic, "Demystifying evals for AI agents"
- OpenAI, SWE-bench Verified
- SWE-bench and Terminal-Bench/Harbor documentation
- METR task-horizon methodology
- Inspect AI documentation
- Anthropic evaluation-infrastructure and agents job descriptions
- Scale AI ACE Agents job description
