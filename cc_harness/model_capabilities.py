"""Verified model capability profiles used for runtime budgeting."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapability:
    model: str
    context_window: int
    source: str
    verified: bool = True
    provider: str = "unknown"
    usage_profile: str = "openai-compatible"
    adapter_scope: str = "runtime-protocol-only"


_REGISTRY = {
    # The specialist contract and current provider preflight use this
    # conservative window. Larger provider-specific windows require an
    # explicit CONTEXT_WINDOW override recorded in activation evidence.
    "deepseek-v4-flash": ModelCapability(
        model="deepseek-v4-flash",
        context_window=128_000,
        source="cc-harness-registry-v1",
        provider="deepseek",
        usage_profile="deepseek-cache-hit-miss",
    ),
    # These entries describe the supported adapter scope, not provider price
    # or a guessed context window.  Until a provider/model is verified by its
    # API metadata, the conservative window is marked unverified and callers
    # must not present it as a provider guarantee.
    "glm": ModelCapability(
        model="glm", context_window=128_000,
        source="cc-harness-adapter-scope-v1", verified=False,
        provider="glm", usage_profile="openai-compatible",
    ),
    "kimi": ModelCapability(
        model="kimi", context_window=128_000,
        source="cc-harness-adapter-scope-v1", verified=False,
        provider="kimi", usage_profile="openai-compatible",
    ),
    "minimax": ModelCapability(
        model="minimax", context_window=128_000,
        source="cc-harness-adapter-scope-v1", verified=False,
        provider="minimax", usage_profile="openai-compatible",
    ),
    "qwen": ModelCapability(
        model="qwen", context_window=128_000,
        source="cc-harness-adapter-scope-v1", verified=False,
        provider="qwen", usage_profile="openai-compatible",
    ),
}


def get_model_capability(model: str) -> ModelCapability | None:
    normalized = model.strip().lower()
    if normalized in _REGISTRY:
        return _REGISTRY[normalized]
    # Compatible endpoints sometimes prefix a provider namespace.
    leaf = normalized.rsplit("/", 1)[-1]
    if leaf in _REGISTRY:
        return _REGISTRY[leaf]
    for provider in ("deepseek", "glm", "kimi", "minimax", "qwen"):
        if leaf.startswith(provider + "-") or leaf.startswith(provider + "."):
            base = _REGISTRY.get(provider if provider != "deepseek" else "deepseek-v4-flash")
            if base is not None:
                return ModelCapability(
                    model=normalized,
                    context_window=base.context_window,
                    source=base.source,
                    verified=base.verified,
                    provider=provider,
                    usage_profile=base.usage_profile,
                    adapter_scope=base.adapter_scope,
                )
    return None
