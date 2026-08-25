#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/terminal_bench_wsl_env.sh"
echo "Terminal-Bench official mode: readiness check only; no task image/verifier is modified."
exec uv run --frozen python scripts/run_cc_only_benchmark.py \
  terminal-bench-2.1 --profile full --check "$@"
