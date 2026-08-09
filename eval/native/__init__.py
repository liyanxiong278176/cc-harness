"""Native deterministic regression contracts and adapters."""

from .adapter import NativePytestAdapter
from .catalog import NATIVE_REGRESSION_DEFINITIONS, install_native_regression_contracts
from .models import NativePytestSpec

__all__ = [
    "NATIVE_REGRESSION_DEFINITIONS",
    "NativePytestAdapter",
    "NativePytestSpec",
    "install_native_regression_contracts",
]
