# Durable Runtime lease model

cc-harness uses three independent lease scopes.  They are persisted in the
project's `runtime.db` and are fenced by epochs, so restarting a process does
not create a second owner for the same durable work.

| Scope | Record | What it protects | What it does not block |
| --- | --- | --- | --- |
| Project | `project_supervisor_lease` | Scheduler leadership for one project | Other Runs under the elected supervisor |
| Run | `run_lease` | One Worker epoch for one Run | Different Runs in the same directory |
| Resource | `run_resource_lease` | A file, workspace, port/named resource, or external side effect | Disjoint resources and shared readers |

The supervisor lease is leader election, not a project-wide mutex.  A single
supervisor can dispatch up to `max_workers` root sessions at once.  Resource
claims are made immediately before a tool action and released after its
durable outcome is recorded:

* read-only actions use shared claims;
* `Write`/`Edit` claims canonical file paths exclusively;
* unknown shell actions claim their working tree exclusively;
* external side effects claim the project exclusively unless the tool supplies
  explicit `resource_keys`.

When a resource is busy, the Worker waits and rechecks the durable lease.  A
cancelled or fenced Run never acquires a new resource.  Expired rows are
removed when a replacement Worker claims its Run, so a crashed process cannot
hold a lock forever.

This gives the expected same-directory behavior: independent sessions can run
in parallel, concurrent reads can share a resource, and overlapping writes
are serialized without stopping unrelated sessions.
