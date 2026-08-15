# ADR 0054: Align LoCoMo category guidance with the frozen fixture

Status: Accepted

Date: 2026-08-15

## Context

The pinned `locomo10` fixture uses numeric categories whose operational meanings are visible in
the questions and answer contract: category 2 asks temporal questions, category 3 asks
evidence-grounded inference or commonsense questions, category 4 requires cross-fact synthesis,
and category 5 is intentionally unanswerable. Earlier project notes incorrectly described category
3 as temporal. The formal scorer already uses the correct category-specific score rules, so changing
the scorer would make results incomparable and would reward metric manipulation rather than better
answers.

## Decision

Keep the primary scorer and existing result contract unchanged. Make retrieval and answer guidance
category-aware instead:

- category 1 preserves exact entities, counts and lists;
- category 2 adds normalized date metadata and temporal ordering constraints;
- category 3 performs evidence-grounded inference without unsupported world knowledge;
- category 4 encourages bounded multi-hop evidence collection and short requested-field answers;
- category 5 requires explicit evidence-grounded abstention when the fixture marks the question
  unanswerable.

The category guidance is a runtime protocol change for the named rerun only. Existing samples remain
immutable evidence; a rerun replaces only the selected sample in the report while retaining its old
attempt for audit.

## Consequences

The temporal implementation is evaluated on category 2, where it can be measured directly. Category
3 improvements are judged by inference accuracy, not by date-specific diagnostics. Retrieval support,
answer quality and infrastructure validity remain separate metrics, and no category score is changed
to compensate for a weak model answer.
