# ADR 0027: Run LoCoMo through the real memory system

Status: Accepted

Date: 2026-08-09

The LoCoMo integration evaluates cc-harness memory rather than reproducing the upstream
conversation-as-context baseline. It ingests each of the ten conversations once in chronological
order through the production L0-L3 memory path, freezes the resulting memory snapshot, and answers
all 1,986 questions as independent queries derived from that snapshot. Query isolation prevents an
earlier benchmark answer from becoming evidence for a later question.

The primary score uses LoCoMo's official deterministic category-specific QA rules: partial F1 for
multi-answer category 1, stemmed token F1 for categories 2 through 4, and the official abstention
rule for category 5. Same-model semantic judging is not part of the primary score. Product-specific
recall, timeliness, utilization and consistency metrics remain separate diagnostics.

Because the ingestion and query interaction differs from the upstream conversation-as-context
protocol, reports identify the result as a `LoCoMo memory-system adaptation`. They do not call it an
official LoCoMo score or compare it directly with results produced under the upstream input
protocol. Existing paired and legacy LoCoMo evidence remains immutable.
