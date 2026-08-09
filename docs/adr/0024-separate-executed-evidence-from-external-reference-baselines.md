# ADR 0024: Separate executed evidence from external reference baselines

Status: Accepted

Date: 2026-08-09

The public benchmark portfolio executes and scores only cc-harness. Published Claude Code scores
may be collected as external reference baselines because rerunning Claude Code locally is outside
the selected cost and execution scope, but differing models, versions, budgets and environments
make those references non-comparable. Reports therefore keep executed evidence and external
references separate and never derive paired deltas, confidence intervals, parity or superiority
claims from the external references.

Existing paired runners and v1-v6 evidence remain immutable. New public benchmark and internal
diagnostic integrations use separate cc-harness-only runners, state contracts and output roots;
they cannot resume from or publish into a paired evaluation namespace.
