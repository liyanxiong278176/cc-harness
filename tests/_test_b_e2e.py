"""Gated real-LLM E2E for the B-stage REPL pipeline.

The filename starts with ``_`` so the normal ``pytest tests/`` suite does not
launch a real provider. Run this file explicitly when OPENAI_API_KEY is set.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


@pytest.mark.requires_llm
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    or os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason=(
        "real LLM gated: set OPENAI_API_KEY and CC_HARNESS_RUN_REAL_LLM=1 "
        "to launch the subprocess"
    ),
)
def test_b_e2e_real_llm_recovers_with_topo_hint(tmp_path: Path):
    """The real REPL can create a todo and then query its DAG view."""
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    env = os.environ.copy()
    env["CC_HARNESS_AUTOCONFIRM"] = "always"
    env["PYTHONIOENCODING"] = "utf-8"
    user_request = (
        "Create one todo task titled 'B stage real E2E probe' with acceptance criterion "
        "'report the DAG', then call todo_toposort and report the DAG topology."
    )

    completed = subprocess.run(
        [sys.executable, str(main_py), "--mode", "coding"],
        input=f"{user_request}\nexit\n",
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    output = completed.stdout + "\n" + completed.stderr

    assert completed.returncode == 0, output
    assert "Traceback (most recent call last)" not in output
    assert "DAG 拓扑视图" in output or "todo_toposort" in output or "topo" in output.lower()
