"""
cc-harness custom Python provider for promptfoo (async version).

Treats a running cc-harness REPL as the "model". For each test case:
  1. spawn `python -u main.py --mode <mode>` (cwd = cc-harness root)
  2. wait briefly for boot output (so MCP / memory init completes)
  3. write the prompt to stdin, then write "exit" to terminate the REPL
  4. read stdout until the process exits (or we kill it on timeout)
  5. parse the captured output, return the "结果" segment as `output`

Why async (not sync):
  promptfoo's Python worker pool uses a Promise.race with a per-call
  timeout (default 5 min, configurable via `timeout:` in the provider
  config — NOTE: in **milliseconds**, not seconds). A sync `call_api`
  still works, but async lets the worker interleave pings while the
  subprocess boots. With the timeout properly set, both work.

Config (set in promptfooconfig.yaml under the provider's `config:`):
  - mode:     "coding" | "plan" | "design"  (default: coding)
  - timeout:  promptfoo worker call timeout in **ms** (e.g. 600000 = 10 min)
  - boot_wait: seconds to wait after spawn before sending input (default: 5)
  - workdir:  absolute path to cc-harness    (auto-detected)
"""
from __future__ import annotations
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# This file lives at: <repo>/eval/promptfoo/wrappers/cc_harness.py
# so the cc-harness root is 3 levels up. We search a few candidates
# upward because promptfoo may invoke us from a context where parents[3]
# resolves to a path without main.py (observed in CI as
# "main.py not found" failures — root cause unclear).
def _resolve_main_py_search(start: Optional[Path] = None) -> Optional[Path]:
    """Search upward from `start` for the first existing main.py.

    Returns the Path to main.py, or None if not found within 6 levels.
    Used both at module import and by tests via the `_resolve_main_py`
    alias below.
    """
    if start is None:
        start = Path(__file__).resolve().parent
    base = start
    for _ in range(6):  # try this dir + 5 ancestors
        candidate = base / "main.py"
        if candidate.exists() and candidate.is_file():
            return candidate
        parent = base.parent
        if parent == base:  # hit filesystem root
            break
        base = parent
    return None


def _resolve_main_py() -> Optional[Path]:
    """Public alias for the search function (used by tests)."""
    return _resolve_main_py_search()


_RESOLVED_MAIN_PY = _resolve_main_py_search()
if _RESOLVED_MAIN_PY is None:
    # Fall back to the original computation; call_api() will surface a
    # descriptive error including the candidates we searched.
    CC_HARNESS_ROOT = Path(__file__).resolve().parents[3]
    MAIN_PY = CC_HARNESS_ROOT / "main.py"
else:
    MAIN_PY = _RESOLVED_MAIN_PY
    CC_HARNESS_ROOT = MAIN_PY.parent


def _resolve_python() -> str:
    """Pick the Python interpreter to use for the cc-harness REPL subprocess.

    Priority:
      1. .venv at CC_HARNESS_ROOT (local dev: Windows .venv\\Scripts\\python.exe
         or Linux .venv/bin/python)
      2. sys.executable (the Python that ran THIS provider — what `pip install
         -e .` would have installed cc-harness into in CI)
      3. `python` on PATH
    """
    import shutil
    venv_py = CC_HARNESS_ROOT / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    if venv_py.exists():
        return str(venv_py)
    # Fall back to whatever Python is running the provider (CI installs
    # cc-harness into the system Python via `pip install -e .`).
    return sys.executable or shutil.which("python") or "python"


# Resolved at provider import time — used by call_api. (Doesn't re-resolve
# per call, so cc-harness root changes mid-test won't be picked up.)
PYTHON_BIN = _resolve_python()

# Substring we look for in the agent's 4-phase output to extract the answer.
_RESULT_MARKERS = ("结果：", "结果:")


async def call_api(prompt: str, options: dict, context: dict) -> dict:
    cfg = options.get("config") or {}
    mode = cfg.get("mode", "coding")
    boot_wait = float(cfg.get("boot_wait", 5))
    # Internal REPL subprocess timeout (separate from promptfoo's worker timeout).
    # Promptfoo's worker timeout is set via `timeout:` in the provider config (ms).
    # Default 300s (5 min) — per-test cap so one pathological probe can't burn
    # the entire job budget. Legitimate P99 is ~2 min, so 5 min = 2.5x headroom.
    # If exceeded, wrapper kills the REPL and returns error; promptfoo records
    # the test as failed and continues to the next probe.
    # Set per-config via `repl_timeout:` if different.
    repl_timeout = int(cfg.get("repl_timeout", 300))
    workdir = Path(cfg.get("workdir") or CC_HARNESS_ROOT)

    if mode not in ("coding", "plan", "design"):
        return {"output": "", "error": f"unknown mode: {mode}"}
    if not MAIN_PY.exists():
        # Show what we searched so debugging is easier in CI.
        candidates = [
            Path(__file__).resolve().parent,
            Path(__file__).resolve().parents[1],
            Path(__file__).resolve().parents[2],
            Path(__file__).resolve().parents[3],
        ]
        searched = " ".join(str(p / "main.py") for p in candidates)
        return {"output": "", "error": f"main.py not found at {MAIN_PY}. Searched: {searched}"}

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            PYTHON_BIN, "-u", str(MAIN_PY), "--mode", mode,
            cwd=str(workdir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as e:
        return {"output": "", "error": f"failed to spawn REPL: {e}"}

    try:
        # Phase 1: let boot complete (MCP + memory init + banner)
        await asyncio.sleep(boot_wait)
        if proc.returncode is not None:
            return await _err(proc, f"REPL died during boot (rc={proc.returncode})", b"")

        # Phase 2: send prompt + "exit"
        try:
            assert proc.stdin is not None
            proc.stdin.write((prompt + "\n").encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.write(b"exit\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            return await _err(proc, f"stdin write failed: {e}", b"")

        # Phase 3: drain stdout until the process exits
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=repl_timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:
                stdout_bytes = b""
            return await _err(proc, f"agent did not complete within {repl_timeout}s (repl_timeout)", stdout_bytes)

        text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""

    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return {"output": "", "error": f"unexpected error: {e}"}
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    answer = _extract_result(text)
    latency_ms = int((time.time() - start) * 1000)
    return {
        "output": answer,
        "latencyMs": latency_ms,
        "tokenUsage": {
            "prompt": len(prompt) // 2,
            "completion": len(answer) // 2,
            "total": (len(prompt) + len(answer)) // 2,
        },
    }


def _extract_result(text: str) -> str:
    """Pick out the agent's '结果：...' segment from the 4-phase REPL output.

    cc-harness prints 思考/行动/观察/结果 segments. We want just the '结果'
    segment. Falls back to the full text if no marker is found.
    """
    for marker in _RESULT_MARKERS:
        idx = text.rfind(marker)
        if idx != -1:
            after = text[idx + len(marker):]
            end = re.search(r"(>\s*\[|^session\s+总计|^本轮)", after, re.MULTILINE)
            if end:
                return after[:end.start()].strip()
            return after.strip()
    return text.strip()


async def _err(proc, msg: str, captured) -> dict:
    if isinstance(captured, bytes):
        try:
            tail = captured.decode("utf-8", errors="replace")[-1500:]
        except Exception:
            tail = repr(captured)[:1500]
    else:
        tail = (captured or "")[-1500:] if isinstance(captured, str) else ""
    try:
        if proc.returncode is None:
            proc.kill()
    except Exception:
        pass
    return {
        "output": "",
        "error": f"{msg}\n--- last output ---\n{tail}",
    }
