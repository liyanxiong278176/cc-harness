# ADR 0083: Align Terminal-Bench 2.1 with the official protocol

Status: accepted. This supersedes the formal-path portions of ADR 0066, 0069, 0079, 0080 and
0081. Their old prewarm and verifier-runtime mechanisms may remain as historical diagnostics but
must not be invoked by a reportable Terminal-Bench 2.1 run.

## Decision

The reportable Terminal-Bench 2.1 path runs the exact pinned 89-task dataset with Harbor's task
images, task resources, task timeouts, verifier scripts and rewards unchanged. Each task runs once.
The single trial, instead of the leaderboard minimum of five, is the only protocol deviation.

The formal path must not pass timeout multipliers, resource overrides, extra Docker Compose files,
host gateway exceptions, offline verifier runtimes, curl replacements or synthetic canaries. The
cc-harness wheel and its agent configuration are agent inputs, not task or verifier replacements.

Harbor errors are retained and count as reward zero. They are not silently retried or removed from
the denominator. The outer scheduler may skip completed tasks after interruption, but it must not
restore or alter state inside an official trial.

## Consequences

- The denominator is always 89 and the primary metric is official single-trial accuracy.
- The result is not leaderboard-compatible because `n_attempts=1`.
- Network or Docker errors can lower the score, matching the official error-handling rule.
- Earlier runs that injected a verifier runtime or changed timeouts are diagnostic only.

## Sources

- https://github.com/harbor-framework/terminal-bench-2-1
- https://github.com/harbor-framework/terminal-bench-2-1/blob/main/leaderboard/SUBMIT.md
