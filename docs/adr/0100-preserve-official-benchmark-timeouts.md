# Preserve official benchmark timeouts

Official benchmark runs use the pinned per-task timeout from the benchmark task manifest as the agent trial wall-clock deadline. The runner must not apply a custom timeout multiplier or override the task, verifier, or environment timeout; internal command and liveness watchdogs may observe activity and emit heartbeats, but cannot extend or replace the official deadline. Local development runs may expose separate command-timeout settings and must be reported as non-official when they differ from the pinned contract.
