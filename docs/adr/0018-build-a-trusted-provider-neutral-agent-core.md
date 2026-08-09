# Build a trusted provider-neutral agent core

## Status

Accepted on 2026-08-03.

Initial implementation status: the runtime now enforces tri-state policy for declared tool paths,
including non-bypassable workspace and credential-path denial. Shell-string containment, sandbox
fail-closed behavior and egress enforcement remain Phase 0 work.

## Context

cc-harness has a capable ReAct loop, project sessions, structured rendering, MCP, memory,
task decomposition and evaluation components. Its executable contracts do not yet match its
documentation: the policy engine has no deny action, sandbox enforcement is incomplete,
core coding operations depend on user-provided MCP servers, compaction mutates persisted
messages, and print mode is not a stable machine protocol.

Copying additional Claude Code UI states before fixing these contracts increases the visible
surface without improving coding reliability. At the same time, binding the core to Anthropic
would discard cc-harness's strongest existing product distinction: provider-neutral BYOK.

## Decision

cc-harness will first build a trusted coding-agent core shared by terminal, headless and SDK
entrypoints.

1. Policy decisions are tri-state: `ALLOW`, `ASK`, and `DENY`. Hard-deny evaluation runs
   before remembered grants, permission modes and policy convenience switches. No confirmation
   handler can convert `DENY` into execution.
2. Read, Edit, Write, Glob, Grep and run_command are first-party tools with one schema,
   path-normalization, event, diff, checkpoint and cancellation contract. MCP remains the
   extension boundary.
3. The append-only session event log is the source of truth. User-visible transcripts and
   model context are projections. Compaction never overwrites original events.
4. Context compilation combines immutable events, complete offloaded blobs, deterministic
   must-retain state and a versioned semantic summary. Snip and Prune affect only a transient
   model projection.
5. Provider/model capability profiles describe context, tools, vision, reasoning, caching,
   structured output and price. Sessions pin a complete harness-profile fingerprint.
6. TUI, JSONL, SDK and later IDE clients consume the same runtime and versioned event stream.
7. A task is successful only when its acceptance criteria have evidence tied to the relevant
   code snapshot. Safety, recovery and data-loss regressions block release.

The nine-area design review completed on 2026-08-03 further fixes these boundaries:

- The agent loop is budget-driven. A high configurable step cap is insurance, not the normal
  stopping rule, and benchmark profiles do not alter production behavior.
- All file mutations compile to one conditional mutation plan. Read returns a content hash;
  Edit and replacement Write reject stale content and commit atomically.
- Offload remains a first-class advantage, but refs become immutable content-addressed artifacts
  and the Mermaid graph becomes a session/branch projection over events.
- Skills, hooks, plugins and MCP remain untrusted extensions. Installation, activation and scoped
  authorization are separate operations.
- Subagents receive persisted task specifications, smaller independent budgets and least-privilege
  capabilities. Parallel writers use isolated worktrees.
- Parity evidence uses development, regression and frozen holdout sets. All same-model comparisons
  use `deepseek-v4-flash` and record the concrete server-returned model version.

## Security ordering

Every tool request follows this order:

1. Parse and validate the typed tool arguments.
2. Normalize every declared path and destination.
3. Evaluate non-bypassable path, credential, sandbox and egress hard-deny rules.
4. Evaluate scoped grants and the ordinary allow/ask policy.
5. Apply the permission mode only to `ASK`.
6. Persist the decision and side-effect attempt before dispatch.

Disabling interactive policy prompts does not disable step 3. Explicit host execution is a
separate user-selected runtime mode and remains subject to hard-deny and audit rules.

## Consequences

- UI parity work is limited to defects that block real usage or verification until the trusted
  core release gate passes.
- Internal APIs and configuration may change during 0.1.x, with migrations for sessions,
  memories, documented CLI behavior and project configuration.
- The event store, context compiler and native tool runtime become shared infrastructure and
  must have platform-specific integration tests.
- Claude configuration may be imported explicitly, but cc-harness formats and security rules
  remain authoritative.
- Durable agent teams, local extensions, web tools, LSP and IDE clients build on the stable
  runtime rather than introducing separate agent loops.
- Rewind directly restores the active state of the same user-visible session. Internally it appends
  a rewind fact and moves the active projection head; only explicit branch commands create a new
  session identity.
- Runtime state moves out of the source tree into a project-isolated user data store. Existing
  project-local databases remain readable until a verified migration succeeds.
