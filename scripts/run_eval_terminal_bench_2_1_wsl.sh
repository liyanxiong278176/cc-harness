#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/terminal_bench_wsl_env.sh"
exec uv run --frozen python scripts/terminal_bench_wsl_supervisor.py "$@"
