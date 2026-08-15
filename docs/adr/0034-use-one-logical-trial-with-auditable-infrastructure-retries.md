# ADR 0034: Use one logical trial with auditable infrastructure retries

Status: Accepted

Date: 2026-08-09

Every portfolio item has one logical trial. A valid official-grader outcome is terminal: `pass` or
`fail` is never automatically rerun. Product failures include grader rejection, agent timeout,
no-progress termination, unrecovered model/tool output errors and safety violations.

Dataset corruption, Docker pull or build failure, provider connectivity or rate limiting, grader
crash and incomplete evidence are `invalid` infrastructure attempts. They receive at most three
automatic attempts with retained evidence, 30-second and 60-second cooldowns, and any longer
provider `Retry-After` value. Exhausted invalid trials do not enter the score denominator; the run
continues but its final report is incomplete. After repairing the infrastructure, `--retry-invalid`
may append new attempts without replacing old evidence.

User cancellation and process interruption are `interrupted`, not valid outcomes. For trials that
do not expose committed item checkpoints, resume rebuilds the item from its frozen initial state;
context-memory trials use the checkpoint-specific rules in ADR 0040. No retry may change the
model, data, grader, permissions or task contract. These rules preserve one-run statistical
semantics while distinguishing product behavior from infrastructure availability.

For checkpointed LoCoMo QA, provider connectivity failures are retried at the smallest safe unit:
only the current uncommitted question, at most three times, restoring the pre-question snapshot
before every retry. Failed child evidence remains under `retry-evidence`. Exhaustion marks the
sample invalid and allows later samples to proceed; a later ordinary invocation reopens the same
logical attempt at that question rather than reingesting the conversation.
