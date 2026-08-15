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

The adapted protocol uses a stable logical memory scope per LoCoMo conversation so restoring the
frozen snapshot into an isolated QA workspace does not hide atoms created during ingestion. A
formal sample is evidence-valid only when ingestion leaves at least one persistent atom and every
QA records an auditable memory-recall activation, including an explicit zero-hit result. Reports
separate execution status, evidence validity and quality metrics. Context projection/utilization
events support claims about basic context management, but not compression superiority.

Retrieval combines vector and FTS/BM25 candidates, applies query entity/date and source-scope
metadata during reranking, and retains injected atom IDs and provenance. One automatic injection
and at most one explicit query expansion form a bounded two-round multi-hop path. Unsupported,
conflicting or low-confidence evidence produces a classified abstention rather than a guess.

Because the ingestion and query interaction differs from the upstream conversation-as-context
protocol, reports identify the result as a `LoCoMo memory-system adaptation`. They do not call it an
official LoCoMo score or compare it directly with results produced under the upstream input
protocol. Existing paired and legacy LoCoMo evidence remains immutable.
