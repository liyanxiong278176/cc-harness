# ADR 0076: Pause Terminal-Bench at a cost boundary

Status: Superseded by ADR 0087

Date: 2026-08-18

Historical note: the earlier Terminal-Bench 2.1 single-pass runner emitted a warning when accumulated estimated cost reached CNY 100 and,
at CNY 200, completes the current task but does not launch another. It never aborts an otherwise valid task
solely because the cost boundary was crossed. The user may explicitly approve a higher budget and resume the
same frozen run; the change and prior pause remain in progress evidence and do not alter scoring protocol.

This tariff-based behavior is no longer used for canonical runs. See ADR 0087:
provider-reported usage and cost are authoritative, and missing direct cost is
reported as unavailable/incomplete rather than estimated.
