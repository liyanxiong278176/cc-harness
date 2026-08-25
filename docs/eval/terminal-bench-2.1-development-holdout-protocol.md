# Terminal-Bench 2.1 development/holdout protocol

The 59 Easy+Medium tasks are a development set. Their retained official verifier
evidence was audited, their failures were inspected, and the project was changed
after observing them. Their 44/59 result is useful engineering evidence, but is
not an untouched test score.

The remaining 30 Hard tasks are frozen in
`eval/harbor/catalogs/terminal_bench_2_1_hard_holdout.json`. They are run once,
with one trial per task, using the official Terminal-Bench 2.1 task images,
instructions, timeouts, and verifier. The run freezes the built cc-harness wheel,
catalog, model identity, and protocol artifacts under its result root before the
first model call.

No task-specific tuning or selective rerun is allowed after the Hard run starts.
Infrastructure interruption may resume the same immutable result root, but a
completed official reward is never replaced. Final reporting separates Easy,
Medium, and Hard, and labels the combined 89-task number as mixed development +
holdout evidence rather than a leaderboard submission.
