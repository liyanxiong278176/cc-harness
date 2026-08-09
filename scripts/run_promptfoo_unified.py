#!/usr/bin/env python3
# Promptfoo unified entry: reads .env, calls run_eval.py unified --keep-json.
# Usage: python scripts/run_promptfoo_unified.py
# Duration: 5-10h.
# Output: D:\agent_learning\cc-harness\eval\promptfoo\unified-report.md
from pathlib import Path
import os
import subprocess
import sys

REPO = Path("D:/agent_learning/cc-harness")
ENV_FILE = REPO / ".env"
PROMPTFOO_DIR = REPO / "eval" / "promptfoo"
RUN_EVAL = PROMPTFOO_DIR / "tools" / "run_eval.py"
PYTHON_EXE = REPO / ".venv" / "Scripts" / "python.exe"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.chdir(PROMPTFOO_DIR)
cmd = [str(PYTHON_EXE), str(RUN_EVAL), "unified", "--keep-json"]
print(f"[runner] cmd: {' '.join(cmd)}", flush=True)
print(f"[runner] cwd: {PROMPTFOO_DIR}", flush=True)
print(f"[runner] OPENAI_API_KEY set: {bool(os.environ.get('OPENAI_API_KEY'))}", flush=True)

rc = subprocess.call(cmd, env=os.environ)
sys.exit(rc)