# Claude Code Gap Matrix

Updated: 2026-08-05

## Scope and evidence

This matrix compares cc-harness with the terminal coding-agent core and headless/SDK
surface of Claude Code. Claude Web, Desktop, Mobile, Slack, Chrome, remote cloud control,
and the broader enterprise platform are outside the current parity boundary.

The pinned Claude Code baseline is v2.1.221. Live execution rejects version drift before running
comparison tasks.
Public capabilities are checked against the official documentation for
[features](https://code.claude.com/docs/en/features-overview),
[tools](https://code.claude.com/docs/en/tools-reference),
[subagents](https://code.claude.com/docs/en/sub-agents),
[agent teams](https://code.claude.com/docs/en/agent-teams),
[hooks](https://code.claude.com/docs/en/hooks),
[skills](https://code.claude.com/docs/en/skills),
[plugins](https://code.claude.com/docs/en/plugins), and
[costs](https://code.claude.com/docs/en/costs).

Statuses describe executable behavior, not README claims:

- **Wired**: reachable from a supported entrypoint and covered by tests.
- **Partial**: useful implementation exists but the user contract is incomplete.
- **Design only**: documented or represented in configuration without an end-to-end path.
- **Missing**: no first-party implementation.

## Capability matrix

| Capability | Claude Code | cc-harness | Status | Priority |
|---|---|---|---|---|
| Terminal agent loop | Mature coding loop with built-in tools | ReAct loop, structured events, queueing, two renderers | Partial | P0 |
| Native file tools | Read, Edit, Write, Glob, Grep | First-party bounded tools with hashes, cursors and transactional mutations are wired into the agent | Wired | P0 |
| Permission engine | Allow, ask, deny, modes and sandbox controls | Tri-state runtime blocks undeclared and credential paths before grants; shell selection fails closed and egress is default-deny, but runtime conformance remains incomplete | Partial | P0 |
| Sandbox | Filesystem/network isolation with platform integration | OpenSandbox is fail closed, host/PTY execution is explicit, and resource, deny-default egress and sensitive-path overlays are SDK-wired; the machine-readable profile withholds `isolated` pending conformance | Partial | P0 |
| Session persistence | Resume, branch and checkpoint workflows | Project sessions, resume, branch, rename, attachments and checkpoints | Partial | P0 |
| Context compaction | Automatic long-session management | Multi-tier compaction and reversible offload, but compaction mutates persisted messages | Partial | P0 |
| Headless protocol | JSON and stream-json output, structured results | Print mode returns final text; no versioned event contract | Missing | P0 |
| Model and cost profile | Claude model selection, usage and cost reporting | Provider-neutral endpoint support, but no authoritative capability/cost profile | Partial | P0 |
| Coding quality regression | Internal evals and production monitoring | Unified evidence contracts, nine-domain native regressions and live canaries exist; full paired suites and the L4 parity gate remain incomplete | Partial | P0 |
| Skills | First-party skill discovery and invocation | No complete skill runtime | Missing | P1 |
| Hooks | Lifecycle hooks with documented events | No unified hook lifecycle or permission manifest | Missing | P1 |
| Plugins | Plugin packaging and marketplace | MCP extensions exist; no package lifecycle or marketplace protocol | Partial | P1 |
| Subagents | Role files, model/tool/permission selection and memory | Single-level fan-out and task integration without role packages or worktree isolation | Partial | P1 |
| Agent teams | Shared tasks and coordinated agents | HTN/DAG foundation, no durable team runtime | Partial | P2 |
| Background work | Background commands, agents and resumable tasks | UI/task concepts exist; no durable daemon or reconnect contract | Design only | P2 |
| Web tools | WebFetch and WebSearch | Available only through optional MCP configuration | Missing | P2 |
| LSP | Language intelligence integrations | No first-party LSP lifecycle | Missing | P2 |
| IDE | First-party editor integrations | No IDE client; runtime events are a useful foundation | Missing | P3 |
| Cross-platform reliability | macOS/Linux/Windows support with known Windows pain points | A three-OS core workflow exists; PTY coverage and saved cross-platform evidence are still incomplete | Partial | P0 |

## Advantages to preserve and wire

| Advantage | Current value | Required work |
|---|---|---|
| Provider-neutral BYOK | Reduces vendor lock-in and enables equal-budget optimization | Add provider/model capability profiles, real context limits, cache and price accounting |
| Structured runtime events | Can support TUI, JSONL, SDK and IDE from one runtime | Version the event schema, persist sequence IDs and add replay cursors |
| Long-term memory | Project memory, reflection, drift and maintenance components exist | Enforce project isolation, provenance, conflict order, cascade forget and quality evals |
| HTN/DAG tasks | Stronger foundation than a flat todo list | Add least-privilege delegation, worktree isolation, durable workers and evidence gates |
| Security evaluation | Red-team, trajectory and output protection components exist | Replace design-only hard safety with enforced DENY and enable CI gates |
| Context offload | Large tool outputs can be replaced with retrievable references | Store complete chunked blobs with hashes; stop truncating refs at 256 KiB |

## Critical truth gaps

1. Hard-deny covers declared tool paths and arbitrary shell execution now fails closed when the
   sandbox is unavailable; full sandbox conformance is still needed before claiming isolation.
2. context compaction mutates the same message list that session persistence later saves.
3. sandbox CPU, memory, egress and credential overlays have one passing Windows Docker report, but
   lack Linux/Kubernetes, external-server and credential-proxy end-to-end evidence.
4. help and renderer surfaces advertise commands whose execution paths differ or are absent.
5. the fast core workflow is enabled, but security evidence is incomplete and no saved
   coding-parity report supports an "exceeds Claude Code" claim.

## Benchmark claim policy

Both harnesses use `deepseek-v4-flash`; Claude Code connects through DeepSeek's
Anthropic-compatible endpoint. Results record the concrete provider model version and a
complete harness-profile fingerprint. A result may be called better only when it meets the
release thresholds in `CONTEXT.md`; safety, data-loss and recovery regressions are never
offset by a composite score.
