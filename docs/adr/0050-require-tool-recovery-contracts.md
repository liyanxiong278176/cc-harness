# Require tool recovery contracts

Every executable tool in the rebuilt runtime will declare its effect class, idempotency semantics, reconciliation support and cancellation support. Scheduling, retry, approval, interruption and crash recovery will consume this contract rather than infer semantics from tool names; third-party tools without trusted metadata default to external side effects that are unsafe to retry, reconcile or confirm as cancelled.
