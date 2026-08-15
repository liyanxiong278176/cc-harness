# Integrate child runs through candidate commits

Each mutating isolated child run will produce one or more local Git commits plus verification evidence as a Candidate Change Set, not write directly into the parent worktree. The parent run will explicitly accept candidate commits in dependency order, resolve conflicts as separate integration work, and re-verify the combined state; this costs additional Git and integration orchestration but makes ownership, rejection, recovery and combined correctness explicit.
