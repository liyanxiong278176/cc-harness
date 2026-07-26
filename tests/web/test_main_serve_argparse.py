"""验证 --serve / --port / --static-dir argparse 注册。"""
import subprocess
import sys


def test_serve_flag_recognized():
    """--serve 不报错(无 sub-command 兼容)。"""
    result = subprocess.run(
        [sys.executable, "main.py", "--serve", "--help"],
        capture_output=True, text=True, timeout=10,
        cwd="D:/agent_learning/cc-harness",
    )
    assert "--serve" in result.stdout or "serve" in result.stdout
    assert "--port" in result.stdout
    assert "--static-dir" in result.stdout
