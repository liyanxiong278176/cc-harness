# ADR 0070: Make Terminal-Bench liveness visible

Status: Accepted

Date: 2026-08-18

The Terminal-Bench 2.1 runner emits a persisted and console-visible heartbeat every 15 seconds. It reports
the task position and ID, execution phase, elapsed time and official timeout remaining, model and tool call
counts, tokens and estimated cost, last event, and state of run-owned child processes. The inner
`run_command` supervisor additionally records CPU ticks, descendant count, process I/O, open sockets, and
bounded workspace file activity; those signals reset the idle budget even when stdout is silent. Training,
build, and service tasks receive bounded class-specific idle budgets, while Harbor's official total task
timeout remains the hard fairness limit. Task completion adds the raw official reward, cumulative success
rate, terminal-outcome counts and cost. ETA is delayed until enough tasks complete and is always labeled as
an estimate.

Lack of new model or terminal output is not sufficient evidence of a hang. A task is stopped only after an
owned process exits unexpectedly, its owned container disappears, checkpoint evidence becomes inconsistent,
or the official timeout expires. Cleanup is ownership-scoped to this evaluation's containers and temporary
resources. This provides useful visibility without changing official timeout or interfering with unrelated
Docker workloads.
