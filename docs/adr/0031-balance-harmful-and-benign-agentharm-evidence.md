# ADR 0031: Balance harmful and benign AgentHarm evidence

Status: Accepted

Date: 2026-08-09

The AgentHarm integration pins the public test release containing eight harm categories, 44 harmful
base behaviors and 44 corresponding benign base behaviors. Each base behavior has four prompt
variants. The `portfolio` profile freezes one variant per base behavior while balancing the four
variant forms and categories, producing 44 harmful and 44 benign runs. The `full` profile runs all
176 harmful and all 176 benign public-test variants.

The primary result exercises the cc-harness `standard` production profile. Any hardened run has a
separate catalog and report and cannot replace default-product evidence. Reports keep harmful
refusal, harmful tool completion, benign task success and benign over-refusal separate. AgentHarm's
simulated tools are preserved, while real network, payment, posting and other external side effects
remain unavailable.

The 32 harmful and 32 benign validation examples are development and grader-calibration data only.
Public-test failures cannot be used to tune the production policy. Because the official refusal and
semantic judges use GPT-4o, the selected `deepseek-v4-flash` judges make this a named non-official
judge adaptation with retained raw judge evidence, consistent with ADR 0028.
