# Use one SQLite WAL event source of truth

The local Durable Runtime will keep one append-only SQLite WAL event stream per project as the authoritative source for Run state. CLI, TUI, workers, and child runs access it through the same Runtime; projections, snapshots, and reports are rebuildable views. This preserves offline use and crash recovery without introducing a second state path or requiring a database service.
