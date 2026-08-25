# ADR 0064: Keep Terminal-Bench official scoring separate from diagnostics

Status: Accepted

Date: 2026-08-18

The Terminal-Bench 2.1 single-pass full report uses the upstream success rule, reward greater than
zero, and divides successful trials by all 89 canonical tasks; errored trials therefore contribute
zero to the official-score view even though the local evidence model also identifies their
infrastructure cause. Raw reward, category performance, validity, token and cost usage, latency,
resume behavior and integrity are reported as separate diagnostics without a custom pooled score.
Because every task runs once, the report does not manufacture leaderboard standard error or
pass@2 through pass@5 values.
