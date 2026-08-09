#!/bin/bash
set -euo pipefail

if [ -f /app/answer.txt ] && [ "$(cat /app/answer.txt)" = "harbor-ready" ]; then
  printf '1' > /logs/verifier/reward.txt
else
  printf '0' > /logs/verifier/reward.txt
fi
