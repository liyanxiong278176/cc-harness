"""Local push checks. Paid Agent evaluations remain explicit commands."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


TESTS = (
    "tests/test_exact_json_contract.py",
    "tests/test_command_failure_evidence.py",
    "tests/test_loop_control.py",
    "tests/runtime_rebuild/test_completion_command_receipts.py",
    "tests/runtime_rebuild/test_live_service_completion.py",
    "tests/runtime_rebuild/test_durable_execution.py",
    "tests/runtime_rebuild/test_runtime_hardening.py",
    "tests/test_prepush_checks.py",
)
SOURCES = (
    "cc_harness/command_failures.py", "cc_harness/exact_json.py",
    "cc_harness/executor.py", "cc_harness/loop_control.py", "cc_harness/worker.py",
    "eval/observability/prepush.py", "scripts/install_observability_hooks.py",
    *TESTS,
)


def main(source: Path | None = None) -> int:
    source = source or Path(__file__).resolve().parents[2]
    env = dict(os.environ, CC_HARNESS_RUN_REAL_LLM="0",
               CC_HARNESS_RUN_SANDBOX_CONFORMANCE="0", CC_HARNESS_OBSERVABILITY_DISABLED="1")
    local_vars = subprocess.run(["git", "rev-parse", "--local-env-vars"], cwd=source,
                                env=env, capture_output=True, text=True)
    if local_vars.returncode:
        return local_vars.returncode
    for name in local_vars.stdout.splitlines():
        env.pop(name, None)
    commands = (
        [sys.executable, "-m", "ruff", "check", *SOURCES],
        [sys.executable, "-m", "pytest", "-q", "-m", "not requires_llm and not sandbox_conformance", *TESTS],
    )
    for command in commands:
        result = subprocess.run(command, cwd=source, env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
