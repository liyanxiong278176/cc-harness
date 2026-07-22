"""Drift detection — 写时+读时双检,运行时 LLM 抽 entity (E5)。

依赖 E2 ReflectionEngine (commit 2c8132a) + E4 maintenance (commit 72b02e4)。
"""
from cc_harness.drift.detector import DriftDetector, DriftVerdict

__all__ = ["DriftDetector", "DriftVerdict"]
