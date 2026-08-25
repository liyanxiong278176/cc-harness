# Schedule work by PlanGraph and project boundaries

Every Run owns a PlanGraph and Plan-Backed Todo; clear work uses one node and complex work may perform read-only discovery before committing its DAG. The Supervisor advances one root Run per project and only claims dependency-ready nodes. Parallel work is limited to path-disjoint, worktree-isolated child nodes selected by the same graph, while child and follow-up Runs receive minimal structured delegation or handoff context and return candidate evidence rather than whole transcripts. Different projects remain independently schedulable.
