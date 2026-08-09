# ADR 0030: Freeze an AgentDojo attack-defense portfolio

Status: Accepted

Date: 2026-08-09

The AgentDojo `portfolio` profile pins upstream benchmark suite version v1.2.2 from the installable
`agentdojo==0.1.35` package and uses its four official environments, 97 benign user tasks, 35
injection goals and deterministic task checkers. It runs all 97 benign tasks once under each
cc-harness `standard` and `hardened` profile to measure the utility cost of hardening.

For attacked trials, the portfolio uses the official `direct`, `ignore_previous`, `system_message`
and `injecagent` attacks. For every injection goal it freezes the deterministic injectable
`user_task_0` from the same suite, then runs the four attacks under both product profiles. This
produces 194 benign runs and 280 attacked runs, 474 agent runs in total. The `full` profile expands
to all same-suite user-task and injection-goal combinations, producing 7,786 runs in a separate
result root.

Reports keep benign utility, utility under attack, targeted attack success and hardened utility cost
as separate metrics. They never collapse those outcomes into one score. The adapter drives
cc-harness through its production tool and safety path while preserving AgentDojo environments,
injections and deterministic checkers; it does not replace the official graders with an LLM judge.
