# Isolate benchmark liveness from the participant

Task-class idle budgets, activity snapshots, official deadline state, and watchdog decisions remain Runtime and operator evidence only. They must not be injected into the benchmark task, model-visible prompt, tool result, or participant-facing interaction surface, because exposing evaluation controls could change agent behavior and compromise comparability. The runner may retain them in restricted audit artifacts and internal monitoring channels.
