# Controlled Specialist Evaluation Readiness

This document covers the controlled Agent loop, Context, Memory and Tools/MCP comparisons between
cc-harness and Claude Code. Both products use `deepseek-v4-flash`. These suites isolate harness
behavior that SWE-bench cannot attribute to one capability domain.

## Zero-call readiness check

Run from Windows Command Prompt:

```cmd
scripts\check_specialist_eval_readiness.cmd
```

The check makes no model API calls and downloads no datasets or Docker images. It writes:

```text
eval/result/specialist-readiness/
  catalog.json
  readiness.json
  readiness.md
  integrity.json
  fixture-smoke/
  context-smoke/
```

It verifies the two executables, the two same-model routes without copying credentials, the local
LoCoMo dataset, free output space, the context corpus generator and a real stdio MCP interaction.
The MCP smoke test proves the frozen `error,error,success` sequence and idempotent side effects.

## Frozen specialist catalog

The version `5.0.0` specialist catalog contains 117 definitions. Every definition declares one
run, a primary domain, fixture types and primary metrics. This single-pass profile is diagnostic:
it measures breadth across frozen tasks but does not estimate within-task stochastic variance.

| Suite | Tasks | Matrix |
|---|---:|---|
| Agent loop | 24 | 6 failure/recovery scenarios x 4 variants |
| Context | 27 | 3 pressure levels x 3 fact positions x 3 scenarios |
| Memory | 34 | 24 project-memory scenarios + 10 LoCoMo conversations |
| Tools/MCP | 32 | 8 semantic capabilities x 4 variants |

Context pressure is 50%, 75% and 90% of the verified model window. Authoritative facts are placed
at 20%, 50% and 80%. Position and pressure are independent variables. Generated corpora record the
tokenizer, measured token count, actual fact offset and content digest.

## Shared fixtures

`eval/specialist/mcp_server.py` is a deterministic local stdio MCP server. It provides fail-first
reads, permanent failure, pagination, strict schema validation, delayed responses, untrusted data
and idempotent mutations. Candidate and baseline receive the same immutable plan but different
state directories, so execution order cannot leak counters or side effects between products.
`eval/specialist/stateful_probe.py` provides the same persisted fail-first and idempotency semantics
for shell/test scenarios that should not depend on MCP tool selection.

`eval/specialist/trajectory.py` maps product-native names to semantic capabilities. For example,
Claude Code `Bash` and cc-harness `run_command` both become `shell`. Raw trajectories remain the
source evidence; normalized events contain argument digests rather than secret-bearing arguments.

## Live single-pass execution

The concrete cases, deterministic graders, multi-phase session adapters and resumable paired runner
are wired. The four domains run independently so failures, interruption state and reports cannot be
mixed. From Windows Command Prompt, start or resume each domain with its own command:

```cmd
scripts\run_specialist_agent_loop.cmd
scripts\run_specialist_context.cmd
scripts\run_specialist_memory.cmd
scripts\run_specialist_tools_mcp.cmd
```

Append `--check` to any command to verify its frozen inputs with zero model calls. The independent
run contracts are:

| Domain | Pairs | Harness trials | Output directory |
|---|---:|---:|---|
| Agent loop | 24 | 48 | `eval/result/specialist-agent-loop24-v5-deepseek-v4-flash` |
| Context | 27 | 54 | `eval/result/specialist-context27-v5-deepseek-v4-flash` |
| Memory | 34 | 68 | `eval/result/specialist-memory34-v5-deepseek-v4-flash` |
| Tools/MCP | 32 | 64 | `eval/result/specialist-tools-mcp32-v5-deepseek-v4-flash` |

Each directory has its own `state.json`, frozen schedule, preflight, raw evidence, normalized bundle,
summary, report and integrity file. `state.json` is written before each harness trial. `Ctrl+C`
terminates the active child process, records that attempt as
`interrupted`, and leaves its trial pending. Running the same command skips completed harness sides
and retries only the unfinished side. Interrupted and failed attempts remain below `raw`.

The evaluation window is frozen at 128,000 tokens for both products. This is an evaluation control,
not a claim about either product's maximum supported window. Normal model-call, tool-call, token and
cost limits are observationally disabled; a two-hour per-phase watchdog remains as emergency stuck
process protection. Provider-transient results may receive at most two completed attempts, while a
user interruption does not consume that allowance.

On completion each runner writes its raw streams, launches, semantic trajectories, deterministic
graders, selected frozen catalog, schedule, state, domain-only normalized bundle, machine summary,
Markdown report and integrity digests. The source catalog digest is
`sha256:44fa47262fc936b0e2cd121bd8857b1c7abfc13eb9c1749463094307ad68a3f6`.

The completed v4 Agent Loop evidence remains under its v4 directory and is never resumed into v5.
No live v5 specialist evidence exists until the corresponding command completes. Because each task runs once, the
result measures controlled breadth but does not estimate within-task stochastic variance and cannot
by itself support a release-level parity or superiority claim.
