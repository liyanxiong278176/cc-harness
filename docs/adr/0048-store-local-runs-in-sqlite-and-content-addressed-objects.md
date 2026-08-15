# Store local runs in SQLite and content-addressed objects

The rebuilt local runtime will use a supervisor-owned SQLite database in WAL mode for run events, leases, approvals, queues and projections, while large tool results, diffs, logs and snapshots live in an atomically written content-addressed object store referenced by digest. Workers submit state transitions through local IPC rather than writing the store directly; this favors transactional recovery, concurrent local reads and zero external infrastructure over multi-host write scalability.
