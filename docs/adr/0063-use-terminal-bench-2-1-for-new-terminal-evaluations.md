# ADR 0063: Use Terminal-Bench 2.1 for new terminal evaluations

Status: Accepted

Date: 2026-08-18

New terminal-capability evaluations use the official 89-task Terminal-Bench 2.1 revision because it
corrects dependency drift, resource mismatches and instruction/verifier defects in 28 Terminal-Bench
2.0 tasks. Existing 2.0 evidence remains immutable historical evidence and cannot be resumed, pooled
or relabeled as 2.1; direct comparison with a 2.0 leaderboard entry requires a separate explicitly
versioned 2.0 run. This supersedes ADR 0032's historical decision to run the 2.0 dataset behind a
2.1-named launcher, while retaining its bounded Docker cleanup and interruption-evidence rules.

The formal local result covers all 89 official tasks once. Official verifier rewards remain the
primary score, and valid pass or fail outcomes are never automatically rerun; the report labels the
result as single-pass full evidence and explicitly not as the five-trial leaderboard protocol.

Infrastructure failures are not benchmark outcomes. After the bounded in-place retries for one task
are exhausted, the runner retains the infrastructure evidence, leaves that task pending, and
continues with the remaining catalog tasks instead of aborting the whole batch. A later invocation
with the same immutable command retries only pending tasks and skips valid pass/fail results. A run
with pending tasks is reported as incomplete and cannot be presented as the 89-task score.
