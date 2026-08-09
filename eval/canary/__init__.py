"""Repeatable same-model canary evaluation for coding harnesses."""

from .adapter import HarnessCanaryAdapter, is_transient_provider_result
from .advanced_catalog import ADVANCED_CANARY_DEFINITIONS, install_advanced_canary_contracts
from .catalog import CANARY_DEFINITIONS, install_canary_contracts
from .live import LiveCanaryResult, default_evidence_root, run_live_advanced_canary
from .models import CanaryInstruction, ProtectedFile
from .retry import (
    PairedAttemptRecord,
    PairedCanaryRunner,
    PairedRetryPolicy,
    PairedTaskSelection,
)

__all__ = [
    "ADVANCED_CANARY_DEFINITIONS",
    "CANARY_DEFINITIONS",
    "CanaryInstruction",
    "HarnessCanaryAdapter",
    "LiveCanaryResult",
    "PairedAttemptRecord",
    "PairedCanaryRunner",
    "PairedRetryPolicy",
    "PairedTaskSelection",
    "ProtectedFile",
    "default_evidence_root",
    "install_advanced_canary_contracts",
    "install_canary_contracts",
    "is_transient_provider_result",
    "run_live_advanced_canary",
]
