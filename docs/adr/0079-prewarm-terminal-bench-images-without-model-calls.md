# ADR 0079: Prewarm Terminal-Bench images without model calls

Status: Accepted

Date: 2026-08-18

## Decision

Provide `scripts\run_prepare_terminal_bench_2_1.cmd` as a model-free preparation path for the
pinned 89-task Terminal-Bench 2.1 catalog. Each task is invoked through Harbor 0.20.0
serially in a disposable container with the full frozen agent-install path and a no-model
agent turn, and is journaled in its own resumable attempt directory. The prewarm does not
use Harbor's `--install-only` shortcut because that shortcut skips the agent setup path.

## Rationale

Harbor's Docker environment names the main image from a content hash of the task environment
definition. The image is therefore reusable across jobs directories, while the task container and
its writable mounts are mutable trial state. Prewarming should retain the image/build cache, not
reuse a mutable container for formal scoring. The formal runner continues to create a fresh
isolated container for each task, preserving benchmark isolation and resumption semantics.

The prewarm launcher strips provider credentials from its host subprocess environment and makes
the agent turn a no-op, so it makes zero model calls while still exercising frozen verifier and
agent installation. It records `manifest.json`,
`catalog.json`, `state.json`, per-task command/log/result evidence, `summary.json`, `report.md` and
`integrity.json`. A completed task is skipped on a repeated invocation; an interrupted task is
resumed in the same logical attempt. `--retry-failed` is explicit for failed environment setup.

## Consequences

- The first 89-task preparation consumes Docker disk and network bandwidth but no model budget.
- The formal run may still perform task setup inside its fresh container; the primary saved cost is
  image pulls/builds, not a promise of zero setup time.
- Prewarm and formal result roots remain separate and are never pooled into the official score.
