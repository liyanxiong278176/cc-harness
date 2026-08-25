# ADR 0056: Expand the balanced AgentDojo slice to 500 trials

Status: Accepted

The 80-trial balanced AgentDojo run was useful for a first live smoke and security report, but its
sample size was too small for stable suite-level evidence. The default AgentDojo portfolio already
contains 474 deterministic tasks (all benign user tasks and all injection goals paired with the
portfolio user task). The balanced catalog is therefore expanded to exactly 500 by retaining those
474 tasks and adding 26 deterministic `user_task_1` attacked trials, distributed across both
standard and hardened tracks and all four suites. This preserves the original 80 task IDs and all
official deterministic checkers while improving breadth without switching to the 7,786-task full
scope.

The expanded catalog receives a new result slug and catalog digest, so the completed 80-trial v2
evidence remains immutable and resumable. `--balanced` selects the 500-trial catalog; the explicit
`--balanced-80` option is retained only for legacy reproduction. No model evaluation is implied by
the zero-call check; the 500-trial result is reportable only after a live run completes.
