# ADR 0026: Preserve RULER protocol and separate context mechanism evaluation

Status: Accepted

Date: 2026-08-09

RULER evaluates long-context retrieval and aggregation by presenting each generated example as the
official one-shot input and applying the official task grader. The cc-harness integration preserves
that interaction pattern and does not split an example into multiple turns or force compaction,
offload or session resume. A portfolio profile may freeze a smaller seeded scope, but its report
must name the exact subset and cannot present it as the official complete RULER score.

Context27 separately evaluates cc-harness context mechanisms, including compaction, offload,
versioned summaries and restoration of a rebuildable context projection. Its mechanism results are
reported independently and are not pooled with RULER scores. This separation avoids changing the
RULER construct in order to expose product-specific context machinery while still testing both
long-context outcome quality and the actual cc-harness context lifecycle.

The frozen RULER `portfolio` profile covers all 13 official task configurations at 32K, 64K and
128K, with five fixed seeded examples for each configuration-length cell: 195 model trials in
total. The `full` profile covers the official six lengths from 4K through 128K with 500 examples
per configuration-length cell: 39,000 trials. The two profiles use independent catalogs, state and
result roots. Reports describe the 195-trial result as a portfolio subset, never as the official
complete RULER score.

The cc-harness-only Context27 `portfolio` profile runs the complete current 27-task matrix once:
three context-pressure levels (50%, 75% and 90%), three authoritative-fact positions (20%, 50%
and 80%), and three scenarios (conflicting sources, distributed constraints, and
compaction-offload-resume). Nine resume tasks have a second phase, producing 36 model phases in
total. Its result root is
`eval/result/context27-v6-cc-only-deepseek-v4-flash/portfolio`; `check`, `portfolio` and `full`
retain independent state and result namespaces. Existing paired Context evidence is not read,
resumed or modified.
