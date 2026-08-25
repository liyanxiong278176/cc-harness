# Batch repeat compaction within each existing tier

Keep the accepted 60% Snip, 80% Prune, and 95% Summary transforms and all existing provenance semantics, but gate repeated publication while the projection remains in the same tier until a meaningful new compressible range has accumulated. Tier entry and escalation remain immediate, so the gate reduces prompt-prefix churn without delaying stronger or emergency compaction.
