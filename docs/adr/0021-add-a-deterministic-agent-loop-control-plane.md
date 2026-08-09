# ADR 0021: Add a deterministic agent-loop control plane

Status: Accepted

Date: 2026-08-08

## Context

The original coding loop was a model-driven ReAct loop. Tool schema validation, policy,
sandboxing, context management and telemetry surrounded the loop, but completion, recovery and
progress remained mostly prompt-driven. A model could emit a final answer without testing a code
change, repeat an action after an unchanged observation, or rely on conversation text to remember
working state.

The v5 specialist Agent Loop run is useful diagnostic evidence but has only 24 one-shot pairs from
six scenario families. It does not justify treating prompt behavior as a deterministic invariant.

## Decision

Add `cc_harness.loop_control` as a provider-neutral control plane around `run_turn`.

1. `CompletionVerifier` rejects a candidate final answer when declared output paths are missing,
   session-owned TODOs remain open, or code changed after the last successful test command.
2. `WorkingState` records the project root, logical working directory, reads, mutations, test
   state, errors and trajectory fingerprints independently of model messages.
3. `RecoveryPolicy` classifies transient, argument, permission, not-found, test and execution
   failures. Only transient failures receive bounded automatic retries; permission denial is
   terminal and cannot be bypassed.
4. `StallController` detects repeated identical action-result trajectories, injects a mandatory
   re-plan observation, and blocks the same action after the threshold.
5. `ToolScheduler` runs only locally proven read-only native tools (`Read`, `Glob`, `Grep`) in
   parallel. Mutations, permission prompts and MCP tools remain ordered and serial.
6. `ActionJournal` writes append-only, fsynced JSONL before and after execution. Content, commands
   and credential-shaped arguments are hashed or redacted before persistence. Restart reconstructs
   working state from the latest committed event and reports interrupted actions. It never blindly
   replays an interrupted mutation.

Every production main-agent, subagent and background-agent path uses the control plane through
`SessionRuntime`. Each agent owns a separate budget, working state and journal, while a declared
`CompletionContract` defines what completion means for coding, research, read-only and delegated
tasks. Only isolated tests and evaluation fixtures may construct a deliberately reduced loop, and
their activation manifests must expose that profile.

## Consequences

- A final model message is now a candidate completion, not proof of completion.
- A transient tool outage can recover without another model call.
- Read-only batches gain concurrency while result messages retain model-request order.
- Crash recovery has auditable action boundaries under
  `.cc-harness/action-journal/<session-id>.jsonl`.
- Existing v1-v5 evaluation evidence is immutable and describes the pre-ADR implementation. A new
  specialist contract/output root is required to measure the effect of this change.
- Completion verification is intentionally conservative. It detects recognized test commands and
  code-file mutations; richer task-specific acceptance checks should be supplied through
  `CompletionContract` rather than inferred from prose.
- Delegation no longer drops deterministic completion, recovery or stall controls at the subagent
  boundary.

## Rejected alternatives

- Prompt-only completion and retry instructions: nondeterministic and not auditable.
- Automatic replay of interrupted writes: risks duplicate or partial side effects.
- Parallelizing MCP tools by name heuristics: MCP schemas do not prove absence of side effects.
- Persisting only conversation messages: cannot reliably reconstruct execution state.
