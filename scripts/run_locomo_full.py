#!/usr/bin/env python3
# LoCoMo 全量统一入口脚本
# 用法: python scripts/run_locomo_full.py
# 时长:5-8h
# 产物:D:\agent_learning\cc-harness\eval\result\locomo-full-2026-07-31\
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

REPO = Path("D:/agent_learning/cc-harness")
PYTHON_EXE = REPO / ".venv" / "Scripts" / "python.exe"
RUNNER = REPO / "eval" / "locomo" / "runner.py"

TS = datetime.now().strftime("%Y%m%d")
OUT_DIR = REPO / "eval" / "result" / f"locomo-full-{TS}"

# mode=coding + 权限 ALWAYS + 编码 + 长 turn + 5-key judge
cmd = [
    str(PYTHON_EXE), str(RUNNER),
    "--output-dir", str(OUT_DIR),
    "--limit", "10",
    "--max-turns", "100",
    "--qa-limit", "30",
    "--no-trace",
]

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["CC_HARNESS_AUTOCONFIRM"] = "always"

print(f"[locomo-full] cmd: {' '.join(cmd)}", flush=True)
print(f"[locomo-full] out: {OUT_DIR}", flush=True)

rc = subprocess.call(cmd, cwd=str(REPO))
sys.exit(rc)