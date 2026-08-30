# Use deadline-aware graceful cancellation

Within the pinned official task deadline, the Runtime starts a bounded graceful-cancellation window for active model and tool work, stops launching new actions, and requests cooperative cancellation. At the exact official deadline it terminates and reaps the entire owned process tree without extending the trial. Any side effect whose final outcome cannot be reconciled is recorded as `outcome_unknown`; it is not replayed unless its Tool Recovery Contract proves the retry safe and idempotent.
