"""Single-system benchmark runtime for auditable cc-harness evaluations."""

from .contracts import (
    MODEL,
    BenchmarkAdapter,
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from .runner import run_benchmark

__all__ = [
    "MODEL",
    "BenchmarkAdapter",
    "BenchmarkTask",
    "CheckResult",
    "EvalProfile",
    "TrialContext",
    "TrialOutcome",
    "TrialStatus",
    "run_benchmark",
]
