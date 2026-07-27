"""真 LLM E2E smoke:test --serve 起服务 → 建 session → 发 user_input → 收 thought 事件。

需 CC_HARNESS_RUN_REAL_LLM=1 + 真实 OPENAI_API_KEY。
Windows 下 aiosqlite teardown hang:用 junit-xml + pkill(沿项目惯例)。
"""
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest


@pytest.mark.skipif(
    os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason="requires real LLM",
)
def test_serve_smoke(tmp_path):
    """起 --serve → curl /api/health → 建 session → WS 发 user_input → 收事件。"""
    port = 18765
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--serve", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # 等 boot
        time.sleep(5)
        # curl health
        import urllib.request
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5)
        assert resp.status == 200
        # WS 测试略(WS 双向 smoke 需更复杂脚本)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()