# cc-harness-only benchmark portfolio

This portfolio executes only cc-harness with `deepseek-v4-flash`. Published Claude Code numbers are
external references, not paired local evidence. Every launcher uses one logical run per task,
retains all infrastructure attempts, and resumes terminal pass/fail tasks after Ctrl+C.

## Windows CMD commands

Run from the repository root:

```cmd
scripts\run_eval_context27.cmd
scripts\run_eval_ruler.cmd
scripts\run_eval_locomo.cmd
scripts\run_eval_longmemeval.cmd
scripts\run_eval_safety8.cmd
scripts\run_eval_agentdojo.cmd
scripts\run_eval_agentharm.cmd
scripts\run_eval_terminal_bench_2_1.cmd
scripts\run_eval_swebench_verified.cmd
scripts\run_eval_final_report.cmd
```

Append `--check` for a zero-model-call prerequisite preparation and check, `--profile full` for
complete upstream scope, or `--retry-invalid` to start a new infrastructure retry generation.
The check may download pinned data, generate check fixtures or build the local wheel, but it never
starts model preflight. Rerun the identical command after Ctrl+C. Results are isolated below
`eval\result\cc-only\<benchmark>\deepseek-v4-flash\<profile>`. Check evidence uses the `check`
result directory while its manifest retains the requested `portfolio` or `full` profile.

## Downloads and protocol adaptations

- AgentDojo installs `agentdojo==0.1.35`; that package contains benchmark suite `v1.2.2`.
- AgentHarm downloads the public-test JSON at revision
  `e23b3fe60a0da9037314b88e5ee3a0c054970dad` and sparse-checks out `inspect_evals` commit
  `b935c0e5cfa04710f016f925db75d8e81413e2cf`.
- AgentHarm uses the pinned simulated tools and grading functions. `deepseek-v4-flash` replaces the
  official GPT-4o refusal and semantic judges, so its score is a named judge adaptation.
- Terminal-Bench's available Harbor registry source is `terminal-bench@2.0`; the historical CMD and
  result slug retain `2.1` but the manifest discloses the actual source.
- RULER preparation downloads and hashes the pinned Git LFS English word list, Paul Graham essays,
  SQuAD v2 and HotpotQA before generating frozen cases. On Windows it invokes the pinned task
  generators with structured argv because upstream `prepare.py` uses `shell=True` with multiline
  templates, which truncates context under CMD. Generated cases are rejected if their reported
  token length is below 75% of the requested RULER length.
- LongMemEval, Harbor tasks and container images require their documented preparation downloads.
  Context27, LoCoMo and Safety8 use repository-local fixtures.

`--check` never performs model preflight or task calls. Live commands require `--confirm-live`; the
CMD launchers supply it. Safety trials are invalid unless the production safety capability reports
enabled, initialized, triggered and non-degraded activation evidence.
