# Detach Durable Agent Runs from terminal clients

Durable Agent Runs will be owned by a local supervisor and executed by leased local workers, while terminal and headless entrypoints act as clients for creation, observation, guidance, approval and cancellation. This adds local process lifecycle, heartbeat and lease complexity, but allows work to survive terminal closure, prevents stale workers from continuing after reassignment, and keeps the first rebuilt runtime local rather than expanding into a web or cloud platform.
