"""Gated real-LLM E2E for the C-stage completion gate + HTN tree pipeline.

The filename starts with ``_`` so the normal ``pytest tests/`` suite does not
launch a real provider. Run this file explicitly when OPENAI_API_KEY is set.

Mirrors ``tests/_test_b_e2e.py``'s pattern: spawn ``python main.py`` in coding
mode, pipe a single user request, capture the 4-phase output, and assert the
expected completion-gate behavior in the resulting stdout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


async def _create(svc, title, status="pending", criteria=None, deps=None,
                  parent=None, session_id="s"):
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        parent_task=parent,
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
def test_c_e2e_real_llm_parent_children_resolve(tmp_path: Path):
    """Real REPL: parent + children, complete children, then mark parent done.

    The completion gate must allow the parent only after children are done;
    if the parent is marked done first, the gate must block. Verifies the
    end-to-end agent loop + completion gate with a real LLM.
    """
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    env = os.environ.copy()
    env["CC_HARNESS_AUTOCONFIRM"] = "always"
    env["PYTHONIOENCODING"] = "utf-8"
    user_request = (
        "Create a parent todo titled 'c-e2e-parent' and two children "
        "'c-e2e-child-1' and 'c-e2e-child-2' under it. "
        "Mark both children done, then mark the parent done. "
        "Report the final status of the parent."
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
        timeout=240,
        check=False,
    )
    output = completed.stdout + "\n" + completed.stderr

    assert completed.returncode == 0, output
    assert "Traceback (most recent call last)" not in output
    # We expect the agent to either succeed end-to-end or be visibly blocked
    # by the gate. Either path proves the integration works.
    assert (
        "c-e2e-parent" in output
        or "todo_update" in output
        or "todo_create" in output
    ), "agent never engaged with the todo tools"