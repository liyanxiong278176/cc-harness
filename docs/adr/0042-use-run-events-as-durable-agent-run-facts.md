# Use run events as Durable Agent Run facts

Durable Agent Run uses an append-only, versioned run-event log as its authoritative history and periodic snapshots only as rebuildable recovery accelerators. This costs more schema, projection and migration machinery than mutable current-state rows, but it is required to distinguish unknown action outcomes after crashes, prevent stale writers from silently overwriting newer state, resume isolated child runs, and support evidence-backed completion with an auditable history.
