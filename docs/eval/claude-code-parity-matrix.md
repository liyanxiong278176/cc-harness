# Claude Code Parity Evaluation Contract

Status: Normative
Version: 1.0.0
Effective: 2026-08-05

This document is the single source of truth for measuring the gap between cc-harness and the
terminal coding-agent core of Claude Code. Other evaluation documents may explain or implement this
contract, but may not redefine its scope, fairness rules or release thresholds.

## Scope

- Candidate: cc-harness.
- Baseline: a pinned Claude Code executable.
- Model: both harnesses request `deepseek-v4-flash` and must report the same resolved model.
- Surface: terminal coding-agent and headless behavior. Claude Web, Desktop, Mobile, Slack, Chrome
  and enterprise administration are outside the comparison.
- Evidence root: every generated run bundle is stored below `eval/result/parity-<UTC timestamp>/`.
- Claim policy: until an L4 release run satisfies this contract, wording is `targets parity with
  Claude Code`, never `matches` or `exceeds Claude Code`.

## Capability Matrix

| Domain | Required task designs | Primary metrics |
|---|---|---|
| Coding Outcome | SWE-bench Verified, Harbor/Terminal-Bench, frozen real-repository bug fixes | deterministic test success, task success, patch correctness, protected-data corruption |
| Agent Loop | multi-step changes, failed tests, malformed/failed tools, repeated no-progress actions, cancellation | completion, model/tool calls, repeated calls, stall rate, recovery rate |
| Context Management | 20%, 50% and 80% context pressure, conflicting documents, compaction, offload and resume | required-fact retention, source attribution, post-compaction success, token reduction |
| Memory | LoCoMo, cross-session project facts, conflict updates, forgetting and project isolation | recall, timeliness, utilization, consistency and contamination |
| Tools & Protocols | Read/Edit/Write/Glob/Grep/shell/MCP composition, truncation and continuation | argument validity, tool selection, edit completeness, boundary violation and error recovery |
| Safety & Privacy | credential access, path escape, prompt injection, unsafe commands and network exfiltration | attack success, hard-deny coverage, secret exposure and false refusal |
| Reliability & Recovery | timeout, crash, cancellation, checkpoint, resume, rollback and unknown side effects | successful recovery, data loss, duplicate side effects and state consistency |
| Human Interaction | clarification, permission prompts, actionable failures, transcript and terminal navigation | correct escalation, interaction completion, unsupported-claim rate and usability defects |
| Operational Fitness | Windows/Linux/macOS, install, upgrade, version pinning, diagnostics and observability | platform pass rate, installation success, version drift, evidence completeness |

Every L4 run covers all nine domains. A task may contribute to multiple domains, but its primary
grader and metrics must be declared before the holdout is frozen.

## Fair Paired Design

Every comparison must satisfy all of these conditions:

1. Use the same content-addressed task contract, repository snapshot, dependency lock, hidden tests,
   locale, timezone, network policy and isolation profile.
2. Use independent clean workspaces. Ordinary coding starts with an empty session, empty long-term
   memory and recorded cache state; only context and memory tasks may inherit declared state.
3. Pin and record both product versions. A configured expected Claude Code version mismatch makes
   the run invalid before task execution.
4. Verify the requested and resolved model during preflight. A missing or different resolved model
   makes the run invalid.
5. Apply the same wall-time, model-call, tool-call, input-token, output-token and cost budgets.
   A harness-specific command-line limit is not evidence that the other dimensions were enforced.
6. Apply the same transient-provider-error classification and synchronized retry policy.
7. Run each task at least three times for release evidence. Randomize the within-pair execution
   order with a recorded seed and balance AB/BA order within each task's repetitions.
8. Mark parser failures, infrastructure failures, provider failures and environmental failures as
   `invalid`; do not count them as an opponent failure. Report invalid rate separately.
9. Keep development, regression and frozen holdout splits separate. Holdout tasks must not tune
   production prompts, policies or defaults.
10. Preserve every trajectory, patch, grader result, stdout/stderr stream, usage record, failure and
    environment fingerprint, including failed and invalid trials.

## Statistical Decision

The unit of inference is the task, not an individual repeated trial. Repetitions remain grouped by
task when resampling.

Required outputs are:

- success rate for each harness;
- paired success-rate difference `cc-harness - Claude Code` with a task-clustered 95% confidence
  interval;
- discordant-pair win rate and Wilson interval as a diagnostic only;
- paired wall-time, token and cost ratios for valid comparable pairs, with task-clustered 95%
  confidence intervals;
- invalid rate and failure taxonomy for each harness;
- per-domain results and vetoes.

The overall outcome is one of:

- `exceeds`: the lower 95% confidence bound of success-rate difference is at least `+0.05`; or the
  lower bound is at least `-0.03` and the upper 95% confidence bound of at least one efficiency
  ratio is at most `0.80`.
- `parity`: the success-rate interval is contained within `[-0.03, +0.03]`, with no qualifying
  efficiency advantage and no hard-gate regression.
- `below`: the upper success-rate bound is below `-0.03`, or Claude Code demonstrates the symmetric
  superiority condition.
- `inconclusive`: sample size, confidence intervals, comparable efficiency evidence or domain
  coverage is insufficient.
- `invalid`: the run contract, evidence integrity, model identity, pinned version or environment
  contract is violated.

Discordant Wilson evidence may support diagnosis but cannot independently produce `exceeds`.

## Hard Gates

An efficiency or aggregate score cannot compensate for any critical regression in:

- safety or credential/privacy boundaries;
- protected-file or user-data integrity;
- cancellation, checkpoint, session recovery or rollback;
- evidence integrity, model identity or product-version identity.

Deterministic graders are primary. An LLM judge may score style, explanation or other genuinely
non-deterministic qualities only when its rubric, version, calibration set and raw judgments are
recorded.

## Suites And Tiers

| Suite | Purpose | Minimum repetitions | Permitted split |
|---|---|---:|---|
| `smoke` | preflight and end-to-end plumbing | 1 | development/regression |
| `dev` | evaluator and strategy development | 1 | development/public |
| `regression` | stable internal capability regressions | 3 | regression |
| `holdout` | frozen unbiased comparison | 3 | holdout |
| `release` | complete nine-domain L4 decision | 3 | regression + holdout evidence |

## Output Contract

Each run writes this immutable projection under `eval/result`:

```text
parity-<timestamp>/
  manifest.json
  schedule.json
  trials/
  trajectories/
  patches/
  scoring/
  summary.json
  parity-report.md
  integrity.json
```

Large content-addressed artifacts may live in a bundle-local object store, but every reference in
`summary.json` must resolve within the run bundle. `integrity.json` records SHA-256 digests for all
decision inputs and report projections.
