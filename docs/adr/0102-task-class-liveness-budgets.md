# Use task-class liveness budgets

The Runtime uses activity-aware idle budgets by task class: 1,200 seconds for training or inference, 900 seconds for build, compile, or service work, and 600 seconds for other work. CPU, process I/O, descendants, sockets, or bounded workspace-file activity resets the idle window and emits a heartbeat. Reaching an idle budget pauses the run for diagnosis as `stalled` rather than declaring business failure; the pinned official task deadline remains the hard upper bound and cannot be extended.
