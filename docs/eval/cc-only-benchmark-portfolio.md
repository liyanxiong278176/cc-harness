# cc-harness-only benchmark portfolio

This portfolio executes only cc-harness with `deepseek-v4-flash`. Published Claude Code numbers are
external references, not paired local evidence. Every launcher uses one logical run per task,
retains all infrastructure attempts, and resumes terminal pass/fail tasks after Ctrl+C.

## Windows CMD commands

Run from the repository root:

```cmd
scripts\run_eval_context27.cmd
scripts\run_eval_longmemeval_context_memory.cmd
scripts\run_eval_locomo_context_memory.cmd
scripts\run_eval_memoryagentbench_context_memory.cmd
scripts\run_eval_context_memory_all.cmd
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
- The unified context-memory domain runs LongMemEval-S Cleaned, LoCoMo and
  MemoryAgentBench with one isolated treatment run per task. Standalone NVIDIA RULER is retired; the
  `ruler_qa1`/`ruler_qa2` sources inside MemoryAgentBench remain official suite members and are not
  reported as standalone RULER.
- MemoryAgentBench requires its pinned resumable preparation download. LongMemEval-S Cleaned and
  LoCoMo use the already pinned local files when their SHA-256 matches. LongMemEval-V2 is retired from
  the active suite because the configured model cannot accept its required image inputs; historical
  V2 data and result evidence remain available for audit but are not runnable or aggregated.

`--check` never performs model preflight or task calls. Live commands require `--confirm-live`; the
CMD launchers supply it. Safety trials are invalid unless the production safety capability reports
enabled, initialized, triggered and non-degraded activation evidence.
