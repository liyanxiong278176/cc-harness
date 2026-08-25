# ADR 0075: Run Terminal-Bench in coding mode

Status: Accepted

Date: 2026-08-18

All Terminal-Bench 2.1 formal tasks use `deepseek-v4-flash` in explicit cc-harness coding mode and the
provider's frozen default thinking behavior. Each task starts a cold session with cross-task long-term memory
disabled. The agent may loop until the unchanged official task timeout instead of receiving a smaller local
model-call or iteration cap. Requested and server-resolved model identity, mode and accepted provider parameters
are recorded and checked on resume.

LoCoMo's chat mode is rejected for this benchmark because Terminal-Bench requires actual filesystem changes,
testing and recovery behavior. Shared sessions, model alias drift, per-task mode selection or changing thinking
behavior would create a different protocol and cannot be mixed into the canonical result.
