# ADR 0025: Use isolated check, portfolio and full benchmark profiles

Status: Accepted

Date: 2026-08-09

Every single-system benchmark integration exposes three isolated execution profiles. `check`
performs prerequisite and contract validation without model calls, `portfolio` runs a frozen
resource-feasible stratified selection and is the default CMD behavior, and `full` runs the complete
upstream scope only when explicitly selected. Each profile has a separate catalog digest, state and
output root, so partial, portfolio and complete results cannot be resumed together or reported as
one score. Reports name the profile and may call a score official only when the upstream protocol
and complete official task scope are preserved.
