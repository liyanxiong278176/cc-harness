# ADR 0055: Add a balanced resumable AgentDojo slice

Status: Accepted

The existing AgentDojo `portfolio` (474 trials) is too large for a security run with a LoCoMo-like
time and cost envelope, while truncating its catalog would overrepresent benign tasks. We therefore
add a fixed 80-trial balanced slice covering all four suites, standard and hardened tracks, benign
utility, two injection goals per suite and all four pinned official attacks. The slice keeps
AgentDojo's deterministic checkers and records utility, attack success, evidence validity, tokens,
cost, latency and trajectories separately; it is explicitly a portfolio subset rather than an
official complete score.

An interrupted trial reuses the same logical attempt and restores the last persisted AgentDojo
environment and tool-call trace. Completed trials remain terminal and are skipped on resume, so a
later invocation does not replay paid work or share state across tasks.

The first live implementation exposed a Windows MAX_PATH failure while creating activation
checkpoints for long attacked-task directory names. That v1 evidence is retained as an audit but is
not scored. The balanced v2 protocol uses bounded, collision-resistant raw directory slugs and a
new result root; its completed 80-trial run produced valid official checker evidence for all 80
trials.

The first v2 report also exposed a grader-state defect: Pydantic `after` validators in AgentDojo's
`Calendar` and `Inbox` rebuilt mutable maps from `initial_*` fields when restoring a checkpoint.
The runner now validates and overlays every persisted model field, preserving dynamic events and
sent mail for both resume and official grading. The existing v2 trajectories were regraded offline,
without new model calls, and the corrected canonical report is 63/80 pass, 15/16 benign utility,
48/64 attacked utility, 0/64 attack success, and 48/64 secure utility. Pre-fix report artifacts
remain under `regrade-audit/pre-fix`.
