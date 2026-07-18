"""Gated real-LLM E2E for D1 dispatch_subagent pipeline.

`_` 前缀 → pytest 默认不收集。需 `OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1` 才跑。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.requires_llm
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    or os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason="real LLM gated: set OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1 to launch",
)
def test_d1_e2e_real_llm_dispatch_subagent(tmp_path: Path):
    """真 REPL:创建 HTN parent + dispatch 2 subagent + parent 标 done。"""
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    env = os.environ.copy()
    env["CC_HARNESS_AUTOCONFIRM"] = "always"
    env["PYTHONIOENCODING"] = "utf-8"
    user_request = (
        "Create a parent todo 'd1-e2e-parent' with 2 children "
        "'d1-e2e-child-1' and 'd1-e2e-child-2'. "
        "Use dispatch_subagent tool to fan-out 2 subagent for the children. "
        "Mark both children done via the subagents, then mark parent done. "
        "Report the parent's final status."
    )
    completed = subprocess.run(
        [sys.executable, str(main_py), "--mode", "coding"],
        input=f"{user_request}\nexit\n",
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, output
    assert "Traceback (most recent call last)" not in output
    assert (
        "d1-e2e-parent" in output
        or "dispatch_subagent" in output
        or "SubAgent fan-out" in output
    ), "agent never engaged with subagent tooling"
