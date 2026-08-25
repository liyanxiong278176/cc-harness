# ADR 0032: Bound Terminal-Bench Docker retention

Status: Superseded by ADR 0063

Date: 2026-08-09

The integration pins Harbor's 89-task `terminal-bench@2.0` dataset at upstream commit
`69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`. Registry inspection on 2026-08-09 confirmed that
Harbor publishes only version `2.0`; there is no `terminal-bench@2.1` dataset. The historical
launcher and result slug retain `2_1`/`2.1` so existing design references do not silently move,
but every manifest and report records the actual `terminal-bench@2.0` source. Its `portfolio` profile
freezes 30 locally supported tasks spanning build and debugging, system administration, data and
machine learning, scientific computing, security and recovery, and multimodal work. The `full`
profile runs all 89 tasks. Each local task runs once; reports state that this is not a leaderboard
submission because the official leaderboard requires at least five trials per task.

Runs are serial. After a task's grader, trajectory and logs are durably published, the runner may
remove only containers and image references proven to have been introduced for that run and task
and no longer in use. It never invokes an unbounded Docker system prune or removes resources that
predate the run. On interruption, evidence is retained; resume reconciles run-owned orphaned
resources and skips completed tasks.

The portfolio result root is
`eval/result/cc-only/terminal-bench-2.1/deepseek-v4-flash/portfolio`. Low disk space pauses the run
as an infrastructure condition and is never scored as a product failure.
