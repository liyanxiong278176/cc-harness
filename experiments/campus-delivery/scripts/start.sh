#!/usr/bin/env bash
# 一键启动: 构建镜像 + 拉起 MySQL/Redis/RabbitMQ/应用
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "[campus] .env 不存在,复制 .env.example -> .env"
  cp .env.example .env
fi

echo "[campus] 校验密钥: APP_JWT_SECRET 必须 ≥32 字节"
JWT_LEN=$(printf '%s' "${APP_JWT_SECRET:-$(grep -E '^APP_JWT_SECRET=' .env | cut -d= -f2-)}" | wc -c)
if [ "$JWT_LEN" -lt 32 ]; then
  echo "[campus] 错误: APP_JWT_SECRET 长度 ${JWT_LEN} < 32,请先在 .env 中配置" >&2
  exit 1
fi

echo "[campus] docker compose up -d --build"
docker compose up -d --build

echo "[campus] 等待服务健康..."
./scripts/healthcheck.sh

echo "[campus] 启动完成。"
echo "  应用:    http://localhost:8080  (GET /api/health)"
echo "  MySQL:   localhost:3306 (campus/campus123456)"
echo "  Redis:   localhost:6379"
echo "  Rabbit:  http://localhost:15672 (campus/campus123456)"
echo "  演示账号: zhangsan / 123456 (用户), m_hanbao / 123456 (商家), rider1 / 123456 (骑手), admin / 123456"
