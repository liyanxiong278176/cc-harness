#!/usr/bin/env bash
set -euo pipefail

# Foreground, official-protocol Terminal-Bench entry point.  This intentionally
# avoids systemd/supervisor indirection so Ctrl+C stops the current run and the
# same output root can be resumed explicitly from its checkpoint.
source "$(dirname "${BASH_SOURCE[0]}")/terminal_bench_wsl_env.sh"
exec uv run --frozen python scripts/run_cc_only_benchmark.py \
  terminal-bench-2.1 \
  --profile full \
  --trials-per-task 5 \
  --confirm-live \
  "$@"
