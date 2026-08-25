# ADR 0066: Gate Terminal-Bench with non-scoring preflights

Status: Accepted

Date: 2026-08-18

Before the Terminal-Bench 2.1 single-pass full run, the runner requires three separately evidenced
non-scoring gates: a zero-model validation of the pinned dataset, 89-task catalog, official scoring
contract and result namespace; a Harbor/Docker check using the official oracle to validate environments
and verifiers without DeepSeek; and one live synthetic terminal task outside the official dataset to
validate model identity, tool calls, trajectory capture and timeout behavior.

The gate outputs are mechanism evidence only and cannot contribute to official accuracy. In particular,
no canonical Terminal-Bench task is used as a disposable live smoke test, so every official task remains
eligible for exactly one scored trial. The Docker prewarm gate now runs all 89 task environments in
install-only mode and records image startup, verifier imports (`pytest`, `ctrf`, `exceptiongroup`, and
`tomli`), `/tests/test.sh` executable/syntax checks, host disk/network readiness, and the task timeout
configuration before any model call. A failed or incomplete gate blocks creation or continuation of paid
formal trials until the gate is repaired and rerun.
