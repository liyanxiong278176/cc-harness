# ADR 0039: Retire LongMemEval-V2 from the active context-memory suite

Status: Accepted

Date: 2026-08-11

The active treatment-only context-memory suite now contains LongMemEval-S Cleaned, LoCoMo and MemoryAgentBench. The V2 adapter, dedicated launcher, preparation path and aggregate membership are removed because the configured `deepseek-v4-flash` fails the required image capability contract; existing V2 data and result evidence remain archived for audit, and reintroducing V2 requires an explicitly image-capable model contract.
