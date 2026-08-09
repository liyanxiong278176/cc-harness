# Trusted Core Roadmap

Updated: 2026-08-04

This roadmap turns the accepted contracts in `CONTEXT.md` and ADR 0018 into independently
verifiable stages. A stage is complete only when its acceptance evidence is saved; feature
presence alone is insufficient.

ADR 0019 fixes the durable storage target. The 2026-08-03 nine-area design review also established
budget-driven stopping, transactional native mutations, capability-based sandbox conformance,
versioned headless events, durable least-privilege tasks, layered extension trust, provenance-bearing
memory and a quality ratchet. These are release contracts, not optional follow-up enhancements.

## Phase 0: Make product claims true

- [x] Add non-bypassable `DENY` for declared tool paths and enforce it before allowlists and
  permission modes.
- [ ] Extend hard safety through the command runner, sandbox and egress layers.
  Uninitialized executor and PTY host-shell bypasses are closed, legacy `enabled=false` no longer
  selects native execution, and sandbox command timeout destroys the container. Sensitive workspace
  paths now receive empty overlays, CPU/memory limits reach the SDK, and egress is default-deny on
  owned `dns+nft` servers. This item remains open until cross-platform runtime conformance proves
  those controls and external servers are rejected or equivalently configured. The first saved
  Windows Docker run (`20260804T093553Z`) passed nine overlay, environment, cgroup, network, OOM and
  cleanup probes; the gated Linux workflow and external-server cases remain open.
- [x] Make sandbox selection fail closed, remove automatic native fallback and require an explicit
  host-execution startup choice.
- [x] Publish a sandbox capability profile and withhold the `isolated` label until filesystem,
  process, resource, network, credential, cancellation and cleanup conformance passes.
- Separate wired capability help from planned capability documentation.
- [ ] Enable a fast cross-platform core/security workflow; keep the multi-hour red-team suite as a
  scheduled or manually triggered evidence job.
- [x] Remove async resource-leak warnings. Session-owned LLM, MCP, memory, L2, session-store and
  executor resources now close explicitly and idempotently; early-exited async streams are closed.
- [x] Establish a recorded Ruff baseline and block new violations while legacy debt is reduced.
  The version-pinned, source-fingerprinted quality ratchet covers production, eval, test and script
  Python code while retaining a zero-warning gate for trusted-core modules.

Exit evidence: bypass mode cannot execute a denied request through interactive, print or runtime
paths; platform tests and audit records prove the decision.

## Phase 1: Native local coding loop

- [x] Implement Read, Edit, Write, Glob and Grep beside run_command.
- [x] Use conditional hashes, one transactional mutation engine, atomic replacement, bounded
  structured results and explicit truncation/continuation metadata.
- Share path rules, structured events, diffs, checkpoints and cancellation across all tools.
- [x] Serialize workspace mutations and allow only independent proven native reads to run
  concurrently. Result messages retain model-request order.
- [x] Add a deterministic loop control plane for completion verification, structured working
  state, classified recovery, repeated-trajectory stopping and append-only action journaling.

Exit evidence: a clean benchmark repository can be inspected, modified and verified without MCP;
conflicting writes fail without losing either change.

## Phase 2: Event-sourced sessions and context

- [ ] Introduce the ADR 0019 project fact store, append-only session events and verifiable migration
  from saved message lists and refs.
- Compatibility layer implemented: user-data-directory fact database, immutable event envelopes,
  content-addressed objects, active-head projection, versioned summaries and read-only legacy import.
  Runtime cutover and legacy ref materialization remain before this item can be checked.
- Build transcript and model-context projections from the event log.
- Replace mutable compaction with reversible offload and versioned summaries.
- Add deterministic context manifests, retention budgets, summary validation, rewind-head
  invalidation and cascade forget.

Exit evidence: resume, crash, compact, rewind and branch tests reproduce the same committed facts;
the raw transcript hash is unchanged by compaction.

## Phase 3: Provider profiles, budgets and headless SDK

- Add provider/model capability and price profiles for `deepseek-v4-flash` first.
- Pin harness profiles per session and make cross-model fallback explicit.
- Publish versioned JSONL events with monotonic IDs, replay cursors and idempotency keys.
- Add token, cost, duration and tool-call budgets with resumable budget stops.
- Replace the production `max_iter=5` stop with budget and no-progress policies; retain only a high
  configurable emergency step cap and independent smaller Subagent budgets.

Exit evidence: TUI and JSONL runs over the same fixture produce equivalent committed event traces;
reported usage reconciles with provider usage.

## Phase 4: Skills, hooks and isolated agents

- Build local extension packages with lockfiles, permission manifests and integrity checks.
- Add explicit Claude configuration import without automatic hook execution.
- Add role-defined Subagents with least-privilege tools, models, memory and budgets.
- Isolate parallel writers in worktrees and persist background permission blocks.

Exit evidence: an extension or child agent cannot exceed its declared capabilities; parallel
changes are reviewed and merged without direct workspace races.

## Phase 5: Web, LSP, durable teams and IDE

- Add controlled WebFetch/WebSearch and language-server lifecycle management.
- Add durable local workers, leases, logs, reconnect and Agent Team coordination.
- Publish a VS Code thin client over the same headless protocol.
- Keep cloud remote control and a hosted marketplace outside the current boundary.

Exit evidence: daemon restart does not duplicate a side effect, lose a task or expose a TCP
listener; IDE and terminal observe the same session.

## Phase 6: Prove parity and advantages

- Run isolated same-model and equal-budget suites against pinned Claude Code.
- Separate public development, internal regression and frozen holdout tasks so production defaults
  cannot be tuned to individual release-gate cases.
- Gate coding success, cost, latency, safety, recovery and cross-platform behavior.
- Run dedicated memory and long-context suites separately from clean coding tasks.
- Publish raw configuration fingerprints, confidence intervals and failure analysis.

Exit evidence: the thresholds in `CONTEXT.md` are met without a safety, data-loss or recovery
regression. Until then, release language says "targets parity" rather than "exceeds Claude Code".
