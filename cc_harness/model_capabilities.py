"""Verified model capability profiles used for runtime budgeting."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapability:
    model: str
    context_window: int
    source: str
    verified: bool = True


_REGISTRY = {
    # The specialist contract and current provider preflight use this
    # conservative window. Larger provider-specific windows require an
    # explicit CONTEXT_WINDOW override recorded in activation evidence.
    "deepseek-v4-flash": ModelCapability(
        model="deepseek-v4-flash",
        context_window=128_000,
        source="cc-harness-registry-v1",
    ),
}


def get_model_capability(model: str) -> ModelCapability | None:
    normalized = model.strip().lower()
    if normalized in _REGISTRY:
        return _REGISTRY[normalized]
    # Compatible endpoints sometimes prefix a provider namespace.
    return _REGISTRY.get(normalized.rsplit("/", 1)[-1])
