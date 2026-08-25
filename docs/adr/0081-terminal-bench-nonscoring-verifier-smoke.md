# ADR 0081: Gate Terminal-Bench with a non-scoring verifier smoke

- Status: Accepted
- Date: 2026-08-21
- Scope: Terminal-Bench 2.1, the pinned 89-task portfolio

## Context

The formal run must report official task outcomes as `pass` or `fail`.  A
missing verifier dependency, an unlaunchable `/tests/test.sh`, a broken
interpreter, or an unavailable network/runtime is not a model score.  Starting
the paid run in that state produces an infrastructure result instead of an
auditable benchmark outcome and may consume model calls before the problem is
visible.

The verifier also legitimately returns a non-zero baseline result before the
agent changes the fresh task workspace.  Treating every non-zero smoke exit as
an environment failure would reject valid task images.

## Decision

The 89-task prewarm performs the same frozen/offline agent installation as a
formal trial, makes the agent turn a no-op, and then runs a bounded verifier
smoke in each fresh container before a formal run. It deliberately does not
use Harbor's `--install-only` shortcut because that shortcut skips agent
setup and can miss deterministic runtime failures:

1. Verify the pinned Harbor host process can import the custom agent before
   Docker work begins.
2. Install the frozen verifier closure and verify all required imports.
3. Require `/tests/test.sh` to exist, be executable, and pass `bash -n`.
4. Run `/tests/test.sh` for at most 300 seconds with model calls disabled.
5. Accept a normal baseline non-zero reward as a non-scoring diagnostic.
6. Reject only startup/runtime markers (missing command/module/file,
   permission, network/TLS, process timeout, port conflict, or equivalent
   deterministic environment failures).
7. Require all selected tasks to pass this gate before the paid formal run.
   The smoke container is disposable; the formal run starts a fresh container
   and only reuses the content-addressed image/build cache.

The smoke result is recorded under each prewarm attempt and in `summary.json`
as `task_preflight.verifier_smoke`; it is never included in the official
Terminal-Bench score.

## Consequences

- A formal 89-task run cannot silently turn an environment defect into a
  model `fail` or an `invalid` trial.
- Prewarm takes longer and may exercise task-local network requirements, but
  it costs zero model tokens and fails before paid evaluation.
- A verifier whose baseline output contains an environment marker is treated
  conservatively as unavailable and must be repaired or rerun in a fresh
  image before scoring.
- Official task files, graders, and initial workspaces are never modified by
  the smoke; only the disposable prewarm container is used.
