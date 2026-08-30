# ADR 0106: Serialize project state writes through one Durable Runtime

Each project has one authoritative Durable Runtime writer for events, leases, projections, snapshots, and compaction publications. Workers, supervisors, and other processes may execute concurrently, but they submit state changes to that writer or use read-only connections; they do not compete as independent SQLite writers. SQLite WAL remains enabled for concurrent reads, while bounded busy retries are only a fallback for unexpected contention.
