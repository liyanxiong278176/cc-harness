# ADR 0071: Retain auditable Terminal-Bench evidence

Status: Accepted

Date: 2026-08-18

Every Terminal-Bench 2.1 task retains its raw Harbor job, official verifier reward and output, ATIF trajectory,
model messages, tool calls, commands, exit codes, timestamps, stdout and stderr, exceptions, timeouts, launch
evidence and all attempts. Operational evidence includes model and tool calls, input/output/cache tokens,
latency, provider-reported cost facts and status, and cc-harness safety, confirmation, recovery and tool-governance events. Dataset,
image, environment and configuration identities are recorded, while credentials and secret environment values
are redacted or omitted.

The runner atomically publishes `manifest.json`, `catalog.json`, `state.json`, `progress.jsonl`, `summary.json`,
`report.md`, `integrity.json` and `raw/`. The report contains official single-pass accuracy, all 89 task results,
category views, failure and infrastructure causes, cost, latency and resume history. These diagnostics remain
separate from official reward and never form a custom composite score. The additional disk usage is accepted
to make the result reproducible, debuggable and suitable for security audit.
