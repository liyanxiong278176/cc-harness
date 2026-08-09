# Advanced Paired Pilot: cc-harness vs Claude Code

Date: 2026-08-05
Model: `deepseek-v4-flash`
Claude Code: `2.1.221`
Evidence: `eval/result/parity-20260805t062613z`

## Design

The pilot ran all three advanced deterministic tasks with three repetitions under a persisted,
within-task AB/BA-balanced schedule. Transient provider failures allowed at most three synchronized
attempts. The run contained nine statistical pairs and 28 materialized trial attempts.

## Result

| Task | Valid pairs | cc-harness passes | Claude Code passes | Final invalid pairs |
|---|---:|---:|---:|---:|
| Cross-file runtime configuration | 3/3 | 3 | 3 | 0 |
| Checkpoint recovery | 2/3 | 3 | 2 | 1 |
| Decision-record context | 3/3 | 3 | 3 | 0 |

All eight valid pairs were ties. The remaining checkpoint pair was invalid after Claude Code hit
the outer 149-second launch timeout in all three attempts. The result is `inconclusive`: only three
task clusters exist, one pair is invalid and checkpoint recovery has fewer than three valid
repetitions.

## Efficiency Diagnostics

| Metric | cc-harness / Claude Code | Task-clustered 95% CI | Interpretation |
|---|---:|---:|---|
| Wall time | 0.896 | 0.679-1.206 | no supported speed advantage |
| Total tokens | 7.251 | 5.910-9.163 | material cc-harness token regression |
| Cost | unavailable | unavailable | cc-harness provider cost telemetry is missing |

Across the selected pair results, cc-harness used 1,003,456 input and 54,488 output tokens. Claude
Code used 80,906 input and 53,022 output tokens. Claude Code reported USD 2.083535; cc-harness cost
remained unknown and was not treated as zero.

## Evaluator Findings

The pilot exposed two evaluator defects before the full regression:

1. An outer process wall-time timeout was labeled as a transient provider failure and retried. This
   could cherry-pick a later valid run and inflate execution cost. Outer wall-time timeout is now a
   non-transient invalid; only evidence containing explicit provider-transient markers is retried.
2. Disposable workspace removal ignored Windows errors and left three timed-out Claude Code
   workspaces. Timeout and cancellation now terminate the process tree. Cleanup retries bounded
   transient errors and turns a persistent cleanup failure into explicit invalid evidence.

The original bundle is unchanged. Its 97 integrity entries match, and it retains all 28 trial,
trajectory and outcome records plus nine scoring selections.

## Decision

Proceed to the 13-task, three-repetition regression only with the corrected evaluator. Do not use
this pilot to claim parity or superiority. The primary product hypothesis from this run is excessive
cc-harness context/token use despite comparable deterministic task success.
