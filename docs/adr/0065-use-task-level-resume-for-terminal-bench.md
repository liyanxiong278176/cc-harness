# ADR 0065: Use task-level resume for Terminal-Bench

Status: Accepted

Date: 2026-08-18

The Terminal-Bench 2.1 single-pass full runner resumes at canonical task boundaries. A restart skips
completed tasks only after their official result and required artifacts pass integrity validation. An
interrupted current task retains its workspace, Harbor jobs directory, runtime context, and last
checkpoint, then reopens the same logical attempt; valid pass and fail results are never rerun
automatically. Infrastructure retries remain attempts of the same logical task and do not increase the
one-trial official denominator. A separate retained infrastructure evidence file is written before the
same attempt is reopened.

The mutable container itself is still never reused as a scoring container: Harbor receives the same
task jobs/workspace namespace and recreates an isolated container from the frozen image. This preserves
the official one-trial denominator while allowing cc-harness checkpoints and deterministic runtime
context to be reused. If Harbor cannot restore the jobs directory, the retained evidence makes the
restart explicit instead of silently pretending that a fresh attempt was a continuation.
