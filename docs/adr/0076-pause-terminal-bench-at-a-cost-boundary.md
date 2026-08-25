# ADR 0076: Pause Terminal-Bench at a cost boundary

Status: Accepted

Date: 2026-08-18

The Terminal-Bench 2.1 single-pass run emits a warning when accumulated estimated cost reaches CNY 100 and,
at CNY 200, completes the current task but does not launch another. It never aborts an otherwise valid task
solely because the cost boundary was crossed. The user may explicitly approve a higher budget and resume the
same frozen run; the change and prior pause remain in progress evidence and do not alter scoring protocol.

Provider-reported usage and cost are authoritative when available. Otherwise the report computes an estimate
from a frozen, versioned DeepSeek tariff and labels it as estimated. Later pricing changes may update attribution
for future calls but never alter official rewards or hide which tariff applied to each run segment.
