# ADR 0072: Use a resume-safe Terminal-Bench namespace

Status: Accepted

Date: 2026-08-18

The canonical paid command is `scripts\run_eval_terminal_bench_2_1.cmd --profile full --confirm-live`, and its
result root is `eval/result/cc-only/terminal-bench-2.1/deepseek-v4-flash/full-single-pass`. If the directory is
absent, the command creates the frozen 89-task run. If it is incomplete and its identity matches exactly, the
command resumes it. If it is complete, the command prints final report paths and exits without another paid
trial. Identity mismatch or integrity failure blocks execution and preserves the existing evidence.

Zero-model, official-oracle and synthetic live gates write to separate `check`, `oracle-preflight` and
`synthetic-canary` namespaces. A deliberate future rerun must use an explicit independently identified new-run
namespace; ordinary execution never overwrites or timestamps around the canonical result. This makes the one
normal command safe to repeat after interruption or completion.
