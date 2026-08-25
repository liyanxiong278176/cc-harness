# ADR 0069: Bound Terminal-Bench infrastructure retries

Status: Accepted

Date: 2026-08-18

The Terminal-Bench 2.1 single-pass runner distinguishes request retry, launcher failure, infrastructure
attempt failure and valid task outcome. Transient rate limits, connection resets, PyPI/TLS disconnects and
provider 5xx responses retry the same frozen task attempt with backoff at most three times, without replaying
committed tool effects.
Failures before the agent starts are retained as launcher evidence and do not consume a formal attempt.

Before Harbor starts a task, the adapter performs a read-only Docker daemon health check with three bounded
probes. A daemon that is still starting is retried before any model call. After agent execution starts, a
confirmed provider, Docker or child-process infrastructure failure permits up to three bounded attempts from
the frozen official initial environment. Missing verifier dependencies,
`command not found`, deterministic runtime defects and confirmed idle-timeout failures are classified as
`environment_not_ready`/deterministic immediately and never enter a paid retry loop. A transient retry is
limited to the configured budget; when it is exhausted, the runner records
`infrastructure-result.json`, leaves the task pending as `infrastructure_pending`, and continues with later
tasks so one outage cannot strand the whole run. It does not publish an infrastructure `invalid` benchmark
result or assign the task a zero. Re-running the same command reuses the same task attempt, workspace and
checkpoints, retaining every prior infrastructure artifact. A finalized report therefore contains only
official `pass`/`fail` outcomes; unresolved environment and infrastructure cases remain explicit and outside
the official denominator.
