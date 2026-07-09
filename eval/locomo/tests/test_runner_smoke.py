import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY = str(REPO / ".venv/Scripts/python.exe")  # abs path; Windows subprocess can't resolve relative


def test_runner_smoke_no_memory_no_trace(tmp_path):
    """--limit 1 --no-trace --no-memory-tools 必须能跑通(不依赖 memory 包恢复 + langfuse)。"""
    if not (REPO / "eval/locomo/data/locomo10.json").exists():
        import pytest
        pytest.skip("locomo10.json not downloaded; run eval/locomo/download_dataset.py")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, str(REPO / "eval/locomo/runner.py"),
         "--limit", "1", "--no-trace", "--no-memory-tools",
         "--output-dir", str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"runner failed:\nSTDOUT={proc.stdout[-500:]}\nSTDERR={proc.stderr[-500:]}"
    html_files = list(tmp_path.glob("locomo-report-*.html"))
    json_files = list(tmp_path.glob("locomo-results-*.json"))
    assert html_files, f"no HTML report; stderr={proc.stderr[-500:]}"
    assert json_files, f"no results JSON; stderr={proc.stderr[-500:]}"