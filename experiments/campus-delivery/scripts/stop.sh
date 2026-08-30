#!/usr/bin/env bash
# 停止并清理(保留数据卷)
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down
echo "[campus] 已停止。如需连数据卷一起清理: docker compose down -v"
