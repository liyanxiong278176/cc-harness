"""Compatibility-facing Terminal-Bench integration.

The reportable evaluator continues to use the audited implementation under
``eval.cc_only``.  This package provides the stable, explicit infrastructure
contracts used by that evaluator without creating a second scoring path.
"""

from .contracts import BenchmarkTask, CheckResult, TrialOutcome, TrialStatus

__all__ = ["BenchmarkTask", "CheckResult", "TrialOutcome", "TrialStatus"]
