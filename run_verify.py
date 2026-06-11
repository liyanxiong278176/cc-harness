"""Run the Phase-1 verification: ask the agent to create + execute hello.py."""
import os
import subprocess
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"D:\agent_learning\cc-harness"
EXE = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
MAIN = os.path.join(ROOT, "main.py")

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

proc = subprocess.Popen(
    [EXE, MAIN],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
    cwd=ROOT,
    text=True,
    encoding="utf-8",
    bufsize=1,
)

output_lines: list[str] = []


def reader():
    assert proc.stdout is not None
    for line in proc.stdout:
        output_lines.append(line)


t = threading.Thread(target=reader, daemon=True)
t.start()

ready_deadline = time.time() + 30
while time.time() < ready_deadline:
    if any("cc-harness ready" in ln for ln in output_lines):
        break
    time.sleep(0.5)
else:
    print("TIMEOUT: REPL never became ready", file=sys.stderr)
    proc.kill()
    sys.exit(1)

print(">>> sending verification command", file=sys.stderr)
assert proc.stdin is not None
proc.stdin.write("在项目根目录创建hello.py并运行它，显示hello world\n")
proc.stdin.flush()

VERIFY_TIMEOUT_S = 90
deadline = time.time() + VERIFY_TIMEOUT_S
while time.time() < deadline:
    if any("shutting down" in ln.lower() for ln in output_lines):
        break
    if "结果:" in "".join(output_lines[-20:]) and any(
        "hello" in ln.lower() for ln in output_lines[-30:]
    ):
        time.sleep(2)
        break
    time.sleep(1)

proc.stdin.write("exit\n")
proc.stdin.flush()

try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

t.join(timeout=2)

print("=" * 60)
print("CAPTURED REPL OUTPUT")
print("=" * 60)
print("".join(output_lines))
