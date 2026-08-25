# ADR 0078: Treat malformed completion markers as recoverable model protocol errors

Status: Accepted

Date: 2026-08-18

The durable runtime may receive a completion marker that is syntactically JSON but does not
contain verifiable `EvidenceRef` objects (for example, an evidence string instead of an evidence
record). The adapter must not turn that provider-format mistake into a worker crash or leave the
run permanently `RUNNING`. It keeps the malformed marker in the assistant text for the next model
turn, omits the completion candidate, and lets the normal segment/retry lifecycle request a repair.

This is a transport/protocol recovery rule, not a relaxation of completion verification: only a
fully parseable candidate that passes the existing evidence and goal checks can produce
`CompletionAccepted`. The malformed output remains in the durable assistant artifact so the event
history and canary diagnostics show why repair was needed.

_Avoid_: accepting string evidence, silently dropping malformed output, retrying a side-effecting
tool because a completion parse failed, or treating a final answer as proof without evidence.
