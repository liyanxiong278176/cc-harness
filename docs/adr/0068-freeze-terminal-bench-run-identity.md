# ADR 0068: Freeze Terminal-Bench run identity

Status: Accepted

Date: 2026-08-18

Every formal Terminal-Bench 2.1 run persists an immutable identity covering the dataset revision and task
digests, Harbor version and exact command, cc-harness Git revision and dirty-state digest, frozen build
artifact SHA-256, verified server-side model identity, non-secret model and safety parameters, catalog,
concurrency, official resources and timeouts, and scoring contract. All 89 tasks execute the frozen build
artifact rather than reading later workspace edits.

Resume verifies that identity before launching a task. Any mismatch is rejected from the existing result
namespace and requires a new run, preventing a score from combining code, data or configuration versions.
The cost is that intentional implementation changes cannot continue an old formal score. Credentials and
secret values are excluded from frozen artifacts and reports.
