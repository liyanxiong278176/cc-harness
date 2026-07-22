"""Drift detection — 写时+读时双检,运行时 LLM 抽 entity (E5)。

依赖 E2 ReflectionEngine (commit 2c8132a) + E4 maintenance (commit 72b02e4)。
T1.1 仅创建子包骨架;DriftDetector / DriftVerdict 在 T1.2 实现。
"""
from __future__ import annotations

# T1.1: DriftDetector / DriftVerdict 暂未实现。try/except 兼容,以便子包
# import 不被 T1.1 阻塞(T1.2 implementer 落 detector.py 后这里会成功 import)。
try:
    from cc_harness.drift.detector import DriftDetector, DriftVerdict
    _HAS_DETECTOR = True
except ImportError:
    DriftDetector = None  # type: ignore[assignment,misc]
    DriftVerdict = None  # type: ignore[assignment,misc]
    _HAS_DETECTOR = False

__all__ = ["DriftDetector", "DriftVerdict"]
