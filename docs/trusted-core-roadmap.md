# Trusted Core Roadmap

Updated: 2026-08-03

This roadmap turns the accepted contracts in `CONTEXT.md` and ADR 0018 into independently
verifiable stages. A stage is complete only when its acceptance evidence is saved; feature
presence alone is insufficient.

## Phase 0: Make product claims true

- [x] Add non-bypassable `DENY` for declared tool paths and enforce it before allowlists and
  permission modes.
- [ ] Extend hard safety through the command runner, sandbox and egress layers.
- Make sandbox selection fail closed and remove automatic native fallback.
- Separate wired capability help from planned capability documentation.
- Enable security and core test workflows; remove async resource-leak warnings.
- Establish a clean Ruff baseline and block new violations while legacy debt is reduced.

Exit evidence: bypass mode cannot execute a denied request through interactive, print or runtime
paths; platform tests and audit records prove the decision.

## Phase 1: Native local coding loop

- Implement Read, Edit, Write, Glob and Grep beside run_command.
- Use conditional hashes, atomic replacement, bounded previews and complete local blobs.
- Share path rules, structured events, diffs, checkpoints and cancellation across all tools.
- Serialize workspace mutations and allow only independent reads to run concurrently.

Exit evidence: a clean benchmark repository can be inspected, modified and verified without MCP;
conflicting writes fail without losing either change.

## Phase 2: Event-sourced sessions and context

- Introduce append-only session events and migrations from saved message lists.
- Build transcript and model-context projections from the event log.
- Replace mutable compaction with reversible offload and versioned summaries.
- Add context retention budgets, summary validation, branch invalidation and cascade forget.

Exit evidence: resume, crash, compact, rewind and branch tests reproduce the same committed facts;
the raw transcript hash is unchanged by compaction.

## Phase 3: Provider profiles, budgets and headless SDK

- Add provider/model capability and price profiles for `deepseek-v4-flash` first.
- Pin harness profiles per session and make cross-model fallback explicit.
- Publish versioned JSONL events with monotonic IDs, replay cursors and idempotency keys.
- Add token, cost, duration and tool-call budgets with resumable budget stops.

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
- Gate coding success, cost, latency, safety, recovery and cross-platform behavior.
- Run dedicated memory and long-context suites separately from clean coding tasks.
- Publish raw configuration fingerprints, confidence intervals and failure analysis.

Exit evidence: the thresholds in `CONTEXT.md` are met without a safety, data-loss or recovery
regression. Until then, release language says "targets parity" rather than "exceeds Claude Code".
