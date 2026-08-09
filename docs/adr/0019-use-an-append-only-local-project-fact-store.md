# Use an append-only local project fact store

## Status

Accepted on 2026-08-03. Implementation is staged behind compatibility adapters.

## Context

The current runtime stores mutable message and renderer-event lists in project-local SQLite tables.
Each save deletes and rewrites a session's rows. Checkpoints duplicate message JSON and file bytes,
while context offload writes project-global refs. This works for a single foreground process, but it
cannot reliably distinguish committed facts from projections, coordinate durable workers, or prove
whether a side effect started before a crash.

Keeping runtime databases inside a repository also creates files merely by opening a project,
interacts poorly with worktrees, and lets repository-controlled paths influence state placement.

## Decision

cc-harness will use a project-isolated store under the user's cc-harness data directory.

1. SQLite WAL stores immutable event envelopes, session metadata, task state, summary metadata and
   artifact references. Event rows are append-only and ordered by a transactionally allocated
   per-session sequence.
2. Large tool outputs, attachments, checkpoint snapshots and offloaded content live in a
   content-addressed object store beside the database. Objects are written and hashed before their
   references commit.
3. A single logical writer serializes event commits for a project. TUI, JSONL, SDK and background
   workers consume the same committed stream.
4. Side-effect attempts are durably recorded before dispatch. A crash without a terminal result is
   recovered as `outcome_unknown`; non-idempotent work is not automatically replayed.
5. Messages, transcripts, context projections, node graphs and task panels are rebuildable views.
   They may be cached but are never the sole source of truth.
6. Rewind appends an event that changes the active projection head without deleting history. Branch
   creates a separate session identity with explicit lineage.
7. Legacy data is imported without inventing missing facts. Unprovable tool, permission and outcome
   details are marked `legacy_unverified`; old refs are hashed into artifacts when available.
8. Backups use the SQLite backup API plus an object manifest. Retention and deletion are based on
   reference reachability and follow the local data lifecycle defined in `CONTEXT.md`.

## Consequences

- Storage and event schemas become public compatibility surfaces and require version adapters.
- SQLite and filesystem commits cannot be one atomic operation; object-first writes may leave safe
  orphans, which garbage collection removes after a grace period.
- The existing project-local store remains available through a read/import adapter during 0.1.x.
  New and legacy formats are not dual-written.
- Moving a project requires explicit identity relinking rather than similarity-based auto-merging.
- Full event-store cutover follows the native tool and sandbox safety slice so rollback remains
  practical while the mutation and attempt contracts are stabilizing.
