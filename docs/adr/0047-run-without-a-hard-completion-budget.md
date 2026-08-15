# Run without a hard completion budget

Durable Agent Runs will not stop because of total token use, model-call count, monetary cost or elapsed duration; they continue until completion, blocking, approval, stalling, cancellation or terminal failure. The runtime will still meter and expose cumulative consumption and enforce operational safety limits such as worker concurrency, per-action timeout, provider rate, storage, hard-deny rules and no-progress circuit breaking, accepting potentially unbounded cost in exchange for genuinely long-running execution.
