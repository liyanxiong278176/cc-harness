# ADR 0041: Add an optional local desktop client without changing the CLI core

Status: Accepted

Date: 2026-08-26

## Context

cc-harness currently provides a terminal-first coding-agent experience through the fullscreen
and classic TUI renderers, with a shared runtime and durable execution path. A desktop package
could improve onboarding, workspace/session navigation, approvals, and visual observability, and
can be distributed as GitHub Release assets without introducing a hosted service or changing the
CLI installation path.

The risk is creating a second implementation of session state, tool permissions, context
compaction, memory, recovery, or cost accounting. That would make the desktop result diverge from
the terminal result and weaken the existing audit and benchmark contracts.

## Decision

Add a desktop client only as an optional local interaction and distribution surface. The first
release target is both Windows and macOS, with Linux kept as a compatible follow-up target. The
shell uses Tauri 2 with React/TypeScript; a supervised Python sidecar exposes a versioned JSONL
stdin/stdout bridge, while the envelope remains transport-neutral for a future local WebSocket or
HTTP/SSE adapter. It must reuse the existing Agent Runtime and its structured event/command
contracts. CLI and TUI remain
the canonical, scriptable entrypoints; the desktop client must not change their command semantics,
session storage, safety boundaries, or model accounting.

The first desktop milestone is an interface shell around existing capabilities: workspace and
session navigation, streaming transcript, command palette, approval prompts, file diff, task
state, and token/cache/direct-cost observability. The information architecture uses a
collapsible three-pane layout: workspace/session navigation on the left, the conversation and
tool stream in the center, and contextual approvals/diff/tasks/usage on the right. Model,
runtime, connection/permission, and usage status belong in a bottom status/input area rather than
in a separate top-level control bar. It does not add cloud sync, a hosted backend, remote
execution, or a second prompt/tool implementation.

The first implementation is a functional vertical slice rather than a static mock: launch and
tray lifecycle on Windows/macOS, workspace selection and projection-only restore, one foreground
conversation with streamed events, tool progress and approvals, explicit stop/continue, bottom
usage/status, and a multi-run navigation list. Diff/task-tree enhancements, settings, and motion
are follow-up work after this path passes parity and safety tests.

On launch, the client may restore the last workspace and session projection, but it must not
automatically call a model, run a tool, resume a queued run, or consume budget. A first launch
shows workspace selection; a restored session requires an explicit user action such as Continue
or Send before execution resumes. Pending approvals, recovery candidates, and stalled runs are
surfaced as actionable bottom status items.

The client may display and supervise multiple runs concurrently because the existing supervisor
already models queued, running, approval-waiting, stalled, and completed states. Each window has
one foreground input target at a time; background runs remain visible in the navigation pane and
can be inspected, approved, interrupted, or resumed without sharing their conversation context.

The desktop process owns a Windows system-tray or macOS menu-bar status item. Closing the main
window hides it and leaves the sidecar and explicitly submitted runs alive. Only an explicit Exit
from that status item ends the desktop process; Exit first reports active runs and requires a
shutdown decision instead of silently cancelling work.

The status item is a projection of durable run state, not a second state machine: idle uses a
neutral mark, active work uses a running mark, approval-waiting uses an attention mark,
stalled/failed runs use an error mark, and an all-clear completed set may use a completion mark.
An activity count or approval badge may be shown without exposing task content. The menu exposes
Open, active-run and approval counts, read-only connection/model status, update checking, and
Exit; destructive bulk-stop actions stay inside the main window behind confirmation.

Approval UX is a projection of the existing action contracts and policy rather than a new
permission system. Read-only actions retain their configured behavior; mutating or external-side
effect actions require an explicit approval card with scope and risk, offer one-time/session
decisions, redact sensitive values, and commit the decision to the same durable event stream used
by CLI/TUI.

## Consequences

- Existing CLI/TUI users and benchmark commands remain unchanged.
- Desktop distribution can use versioned Windows and macOS GitHub Release assets and does not
  require a hosted service or a new registration flow.
- The desktop client needs a stable local runtime boundary and structured event protocol before
  broad UI work begins.
- Desktop-specific packaging, auto-update, signing, and OS credential storage become release
  responsibilities.
- Any desktop-only behavior must be covered by the same capability-continuity and safety tests as
  the terminal path.
