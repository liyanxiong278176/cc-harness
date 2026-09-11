"""Official Terminal-Bench 2.1 catalog facade.

The audited catalog implementation remains the single source of truth; this
module exposes it under the rebuilt package name for tooling and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.cc_only.adapters.harbor import TERMINAL_BENCH_21_DATASET, TerminalBenchAdapter
from eval.cc_only.contracts import BenchmarkTask, EvalProfile


def load_catalog(project_root: Path, profile: EvalProfile = EvalProfile.FULL) -> tuple[BenchmarkTask, ...]:
    """Load and validate the frozen official catalog through the audited adapter."""

    return tuple(TerminalBenchAdapter().catalog(project_root, profile))


def catalog_identity() -> dict[str, Any]:
    """Return the immutable dataset identity used by formal runs."""

    return {"dataset": TERMINAL_BENCH_21_DATASET, "task_count": 89}


__all__ = ["catalog_identity", "load_catalog"]
