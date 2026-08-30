#!/usr/bin/env bash
# 健康检查: 应用 /api/health + 各中间件容器健康状态
set -euo pipefail
cd "$(dirname "$0")/.."

MAX_WAIT=${HEALTH_MAX_WAIT:-120}
echo "[campus] 健康检查(最长等待 ${MAX_WAIT}s)..."
wait=0
while [ "$wait" -lt "$MAX_WAIT" ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/health 2>/dev/null || true)
  if [ "$code" = "200" ]; then
    echo "[campus] 应用健康: GET /api/health -> 200"
    break
  fi
  sleep 3
  wait=$((wait + 3))
done

if [ "$code" != "200" ]; then
  echo "[campus] 错误: 应用 ${MAX_WAIT}s 内未就绪 (last http=$code)" >&2
  echo "[campus] 排查: docker compose ps; docker compose logs app; 检查 .env 中 APP_JWT_SECRET" >&2
  exit 1
fi

echo "[campus] 中间件容器状态:"
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Health}}' || true

echo "[campus] 健康检查通过。"
