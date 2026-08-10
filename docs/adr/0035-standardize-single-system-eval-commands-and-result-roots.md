# ADR 0035: Standardize single-system eval commands and result roots

Status: Accepted

Date: 2026-08-09

Each new cc-harness-only integration has one Windows CMD entrypoint:

- `scripts\run_eval_context27.cmd`
- `scripts\run_eval_locomo.cmd`
- `scripts\run_eval_longmemeval.cmd`
- `scripts\run_eval_safety8.cmd`
- `scripts\run_eval_agentdojo.cmd`
- `scripts\run_eval_agentharm.cmd`
- `scripts\run_eval_terminal_bench_2_1.cmd`
- `scripts\run_eval_swebench_verified.cmd`
- `scripts\run_eval_final_report.cmd`

Benchmark launchers default to `--profile portfolio` and share `--check`, `--profile portfolio`,
`--profile full` and `--retry-invalid`. User interruption returns a distinct interrupted status;
running the identical command resumes from persisted state.

New evidence uses
`eval/result/cc-only/<benchmark>/deepseek-v4-flash/<profile>/`. Benchmark slugs are `context27-v6`,
`locomo-memory`, `longmemeval-s-cleaned`, `safety8`, `agentdojo-v1.2.2`,
`agentharm-public-test`, `terminal-bench-2.1`, `swe-bench-verified` and `final-report`. Safety8 stores
`standard` and `hardened` below the selected profile. Protocol and judge adaptations remain in the
run manifest rather than being inferred only from directory names.

This namespace decision supersedes the not-yet-created flat output roots named in ADRs 0032
and 0033. It does not move, rewrite or resume any existing paired, specialist or Harbor evidence.
