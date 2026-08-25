# ADR 0067: Run Terminal-Bench serially

Status: Accepted

Date: 2026-08-18

The canonical Terminal-Bench 2.1 single-pass full run uses concurrency one and leaves each official task's
CPU, memory and timeout configuration unchanged. This reduces Docker resource contention, provider bursts
and ambiguous timeout failures on the Windows host, and makes task-boundary cleanup and resume evidence
deterministic. The accepted trade-off is a multi-day wall-clock run.

Concurrency is included in the immutable run contract. Diagnostic runners may expose other values only in
separate adapted namespaces; they cannot resume into, pool with or replace the canonical serial result.
