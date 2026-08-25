# ADR 0077: Make Terminal-Bench launchers version explicit

Status: Accepted

Date: 2026-08-18

`scripts\run_eval_terminal_bench_2_1.cmd` is corrected to resolve only the official
`terminal-bench/terminal-bench-2-1` dataset and to write the new 2.1 `full-single-pass` namespace. Historical
evidence produced while the 2.1-named launcher still selected `terminal-bench@2.0` remains immutable, explicitly
identified as 2.0, and cannot resume into or substantiate a 2.1 result.

If legacy 2.0 execution remains useful, it receives the unambiguous
`scripts\run_eval_terminal_bench_2_0.cmd` entry point and a separate result identity. Manifests validate the
resolved dataset revision and per-task digests rather than inferring version from the launcher filename. This
migration preserves audit history while removing the misleading active command.
