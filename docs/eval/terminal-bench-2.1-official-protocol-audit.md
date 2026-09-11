# Terminal-Bench 2.1 official protocol audit

## Verdict

The reportable path uses the pinned official Harbor dataset
`terminal-bench/terminal-bench-2-1@sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`
and Harbor's official task images, task resources, timeouts, test scripts, and
rewards. The only declared benchmark deviation is one trial per task instead
of the leaderboard minimum of five.

The custom cc-harness agent may receive a private read-only Python runtime
because some official task images do not contain a compatible Python. This is
an agent installation input supported by Harbor's custom-agent interface. The
mount is restricted to `/opt/cc-harness/agent-runtime`,
`/opt/cc-harness/agent-site`, and `/root/.local/bin/cc-harness`.

The formal path fails closed if an overlay changes `PATH`, mounts `/tests`,
replaces `apt-get`, `curl`, `uv`, `uvx`, Python, or uses timeout/resource/host
overrides. Each scored attempt records `official-protocol.json`.

The formal path does not add a synthetic canary or a separate verifier-network
soak before Harbor. Those checks are available as explicit diagnostics only;
otherwise a transient failure in an unrelated probe could block an otherwise
valid official task. The launcher and Harbor child now receive the same
explicit `DOCKER_HOST=unix:///var/run/docker.sock` (when no host is supplied)
and an empty `DOCKER_CONTEXT`, so a stale `desktop-linux` context cannot make
the launcher probe and Harbor's own Docker preflight disagree.

The local Durable Runtime completion synthesizer remains strict for normal
user runs: it needs successful verification-shaped evidence before it can mark
the run completed. In the isolated official benchmark path it may instead
commit a candidate from successful tool observations when no action is still
pending. This is necessary for official tasks whose verifier is the first
authoritative test (for example, repository-recovery tasks where the agent may
not run a command containing `test`). The benchmark-only exception requires
both `CC_HARNESS_TERMINAL_BENCH=1` and the explicit trusted-task provenance;
Harbor's unchanged `/tests/test.sh` and reward remain the final oracle.

## Invalidated diagnostic run

The earlier Hard result root used an offline verifier overlay that replaced
bootstrap commands. It is not an official-protocol score and was archived as:

`eval/result/cc-only/terminal-bench-2.1/deepseek-v4-flash/_superseded-protocol-invalid/260824-full-selected-d8fc583247-offline-verifier`

No result from that root may be merged into the Hard holdout or the 89-task
summary.

## Verification

- Frozen catalog: 89 official tasks at the pinned dataset digest.
- Zero-model readiness check: `ready`.
- Model calls during the check: `0`.
- Official verifier unmodified: `true`.
- Custom agent runtime ready: `true`.
- Formal protocol audit: `pass`.
- Overlay scope: `agent-runtime-only`.

The selected Hard root is now empty and must be created by the first valid
single-trial run after explicit user approval.
