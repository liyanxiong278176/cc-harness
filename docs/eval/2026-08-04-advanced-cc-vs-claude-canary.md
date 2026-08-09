# Advanced cc-harness vs Claude Code Canary

Date: 2026-08-04
Model: `deepseek-v4-flash` on both harnesses
Scope: cross-file configuration, checkpoint recovery and decision-record context tasks on Windows

## Original Result

| Task | cc-harness | Claude Code | Pair outcome |
|---|---:|---:|---|
| `canary.advanced.cross-file-runtime-config` | pass | pass | tie |
| `canary.advanced.checkpoint-recovery` | pass | invalid | invalid |
| `canary.advanced.decision-record-context` | fail | pass | Claude Code win |

The paired result is inconclusive: zero cc-harness wins, one Claude Code win, one tie and one
invalid pair. Only one discordant valid pair exists, below the required minimum of ten.

The immutable evidence root is
`.cc-harness/eval/advanced-20260804t055459z`. Its summary digest is
`sha256:73bdd70166b8e038a43a45add7ce4b4c7ea2dc4f05a11fbe3e6a51276139c473`
and its report digest is
`sha256:a9d1d421af81444145dddc46ae2ec62997b5a2e105cf74a4e3f737568362e432`.

Claude Code exceeded its 149-second launch budget in both checkpoint-recovery attempts, so that
pair has no selected result. The attempts remain in the journal. The earlier
`.cc-harness/eval/advanced-20260804t054630z` run was intentionally aborted after discovering an
incorrect 20-model-call contract and is not benchmark evidence.

## Context Failure Root Cause

cc-harness found the accepted decision record and prepared the correct file write. The production
agent loop nevertheless used a five-iteration default. Its max-iteration guard returned before the
pending write tool call could execute, leaving `src/retention.py` unchanged. The grader failure was
therefore an agent-loop control bug, not a context retrieval error.

The loop now executes tool calls returned by the final allowed model response before stopping; the
budget limits model calls rather than discarding already-authorized work. The runtime also defaults
to 20 iterations, exposes a bounded `--max-iterations` option and accepts the Task Contract's
`max_model_calls` value in live eval launches. Focused tests cover both the runtime wiring and the
final-call dispatch boundary.

## Post-fix Regression

Only `canary.advanced.decision-record-context` was rerun. Both harnesses passed, producing a tie.
cc-harness made seven model calls; its write executed on iteration six and changed the final source
digest to `sha256:d3662f4b2ab137791588771eb5ce8823c41a13adaa412739b57bfd8a97411b74`.
The grader exited zero and protected tests remained unchanged.

The immutable post-fix evidence root is
`.cc-harness/eval/advanced-20260804t061809z`. Its summary digest is
`sha256:94886f63eefd3d23b10758989d24eea86bbf57ab586b202cfe78a1cdb68b25c7`
and its report digest is
`sha256:c5e37e0b83d217c90909ef3cd503be8c52b1defd0060edcc59cebf1c01e948a3`.
The original result remains unchanged.

## Telemetry Note

The reports created during these runs show zero Claude Code tool calls because the original parser
only consumed explicit aggregate `tool_calls` fields. Claude stream JSON represents calls as
`tool_use` content blocks. The parser now counts unique assistant `tool_use` IDs and ignores tool
results and task notifications. Replaying the immutable post-fix stream yields 11 Claude Code tool
calls. Stored summaries are not rewritten; corrected counting applies to future runs.

## Interpretation

The regression closes one concrete agent-loop defect but does not establish parity or superiority.
The advanced sample is too small, checkpoint recovery lacks a jointly valid pair and cc-harness
still uses substantially more input tokens on the selected tasks. The next evidence run should add
more high-risk tasks, isolate launch timeout from provider retry time and collect enough discordant
valid pairs for a meaningful paired interval.
