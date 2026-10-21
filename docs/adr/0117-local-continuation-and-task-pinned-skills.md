# ADR 0117: Rebuild local continuations and pin skills to Run events

Status: Accepted

Date: 2026-10-21

## Context

The local coding agent already owns event-backed execution, compaction and layered memory.
Long tasks still need an inspectable continuation contract, and user-editable workflows must
not change silently midway through a task. A separate authoritative handoff file or mutable
skill pointer would make crash recovery disagree with the Run event stream.

## Decision

Keep Run events as task authority. Generate machine-readable continuations and human-readable
handoffs from committed state. Check the active model view against that state, reconstruct a
missing view once without executing tools, and wait for input if integrity or window protection
still fails. Historical permission snapshots never override current permission checks.

Discover skills from project and user directories, require explicit enablement, and resolve
same-name sources explicitly. Snapshot instructions and resources into content-addressed objects.
`SkillSnapshotPinned` events select the task's snapshot; local registry rows are discovery
configuration and caches, not authority over an already-pinned task. Updates affect future tasks;
explicit reload requires a safe task boundary and records a new revision in the event stream.

Automatic long-term facts require user preference/decision evidence or a successful tool
observation. Keep speculative model conclusions out of automatic fact storage. Forgetting
withdraws memory versions, merged derivatives, higher-layer views and injection snapshots while
retaining independently governed original events; a withdrawal epoch fences stale writers.

Expose one local management service through WebUI and command entrypoints. Restore missing
references only from matching bytes within the current project. Preview cleanup and protect
event, checkpoint, skill and nested manifest references before deleting old orphan objects.
No tenant service, hosted registry, enterprise knowledge stack or distributed database is added.

## Consequences

- Editable handoffs are useful to humans but cannot rewrite tool outcomes, approvals or goals.
- Fixed skill packages increase local storage in exchange for reproducible task continuation.
- A forgotten source may conservatively invalidate an entire merged conclusion or higher-layer
  view; surviving source atoms can rebuild the remaining view later.
- Verified automatic facts remain separate rather than being rewritten into unverified merged
  sentences. Speculative reflection can remain a task trace without becoming a long-term fact.
- Original history may still contain forgotten information. Forgetting is not history destruction,
  external backup deletion or a claim of physical secure erasure.
