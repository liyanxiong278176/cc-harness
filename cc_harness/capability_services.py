"""Shared capability configuration and lifecycle interfaces.

The interactive and durable entrypoints must not silently assemble different
security/context stacks.  This module owns the common configuration boundary;
the concrete runtimes still own their run/session-scoped resources.
"""

from __future__ import annotations

import os
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from openai import AsyncOpenAI

from .activation import CapabilityProfile
from .config import (
    AppConfig,
    ContextConfig,
    ExecutorBackend,
    ExecutorConfig,
    L2Config,
    load_context_config,
    load_executor_config,
    load_l2_config,
    load_l5_config,
    load_policy_config,
)
from .l5 import build_l5_engine
from .memory.config import load_memory_config
from .policy import PolicyEngine


def _module_available(name: str) -> bool:
    """Return whether an optional runtime module can be imported.

    ``find_spec`` can itself raise for partially-installed distributions (or
    modules whose ``__spec__`` has been cleared by a test/plugin).  Capability
    discovery must never make startup fail, so those cases are reported as
    unavailable and surfaced in the activation manifest instead.
    """

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
        return False


class ContextEngine(Protocol):
    """Shared context projection/compaction contract."""

    async def build_context(
        self,
        projection: Any,
        base_messages: Sequence[Mapping[str, Any]],
        tool_specs: Sequence[Mapping[str, Any]],
        *,
        query: str = "",
    ) -> Any: ...


class MemoryEngine(Protocol):
    """Shared memory capture/recall/checkpoint contract."""

    async def checkpoint_memory(
        self,
        run_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        segment: int,
        committed_progress: bool,
    ) -> Any: ...


class SafetyEngine(Protocol):
    """Policy, input screening, output DLP, and provenance boundary."""

    async def validate_goal(self, projection: Any) -> tuple[bool, str]: ...

    def protect_model_output(
        self,
        text: str,
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[str, Mapping[str, Any]]: ...


class ToolRuntime(Protocol):
    """Provider-neutral tool dispatch contract."""

    async def execute_tool(self, request: Any) -> Any: ...


class PlanningRuntime(Protocol):
    """Plan/Todo scheduling authority contract."""

    async def plan_is_ready(self, run_id: str) -> bool: ...


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class SharedCapabilityServices:
    """Configuration shared by SessionRuntime and DurableRuntime.

    Resource ownership remains with the caller.  In particular, the returned
    L2 client is closed by the runtime that constructed this bundle.
    """

    cwd: Path
    config: AppConfig
    policy: PolicyEngine
    context_config: ContextConfig
    memory_config: Any
    l2_config: L2Config
    l2_client: Any | None
    l2_model: str
    l5: Any | None
    executor_config: ExecutorConfig
    e1_decompose_enabled: bool
    provenance_mode: bool
    output_egress_guard_enabled: bool
    l5_config: Any | None = None

    @classmethod
    def load(
        cls,
        cwd: Path,
        config: AppConfig,
        *,
        profile: CapabilityProfile | None = None,
        additional_roots: Sequence[Path] = (),
        host_execution: bool = False,
        context_config: ContextConfig | None = None,
    ) -> "SharedCapabilityServices":
        root = Path(cwd).resolve()
        policy_path = root / "policy.yaml"
        policy_config = load_policy_config(policy_path)
        profile_name = profile.name if profile is not None else "standard"
        provenance_mode = profile_name == "hardened-safety" or os.getenv(
            "CC_HARNESS_SECURITY_MODE", ""
        ).strip().lower() in {"strict", "hardened", "security"}
        policy = PolicyEngine(
            project_root=root,
            enabled=policy_config.enabled,
            additional_roots=additional_roots,
            provenance_mode=provenance_mode,
        )
        l2_config = load_l2_config(policy_path)
        l2_client = (
            AsyncOpenAI(api_key=config.openai_api_key, base_url=config.openai_base_url)
            if l2_config.enabled and config.openai_api_key
            else None
        )
        executor_config = load_executor_config(policy_path)
        l5_config = load_l5_config(policy_path)
        if host_execution:
            executor_config.backend = ExecutorBackend.NATIVE
        return cls(
            cwd=root,
            config=config,
            policy=policy,
            context_config=context_config
            or load_context_config(
                model=config.openai_model,
                environ=config.runtime_environment,
            ),
            memory_config=load_memory_config(
                policy_path,
                environ=config.runtime_environment,
            ),
            l2_config=l2_config,
            l2_client=l2_client,
            l2_model=os.getenv("JUDGE_MODEL") or config.openai_model,
            l5=build_l5_engine(l5_config),
            executor_config=executor_config,
            e1_decompose_enabled=policy_config.e1_decompose_enabled,
            provenance_mode=provenance_mode,
            output_egress_guard_enabled=(
                profile_name == "hardened-safety" or _env_flag("CC_HARNESS_OUTPUT_EGRESS_GUARD")
            ),
            l5_config=l5_config,
        )

    def activation_details(self) -> dict[str, Any]:
        """Return redaction-safe activation metadata for this shared stack."""

        sandbox_sdk = _module_available("opensandbox")
        sandbox_server = _module_available("opensandbox_server")
        sandbox_requested = self.executor_config.backend is ExecutorBackend.SANDBOX
        l5_config = self.l5_config
        pii_requested = bool(getattr(l5_config, "enabled", False) and getattr(l5_config, "pii_on", False))
        pii_dependency = _module_available("presidio_analyzer")
        memory_enabled = bool(getattr(self.memory_config, "enabled", False))
        memory_dependency = _module_available("sqlite_vec")
        degraded: list[str] = []
        if sandbox_requested and not sandbox_sdk:
            degraded.append("sandbox_sdk_missing")
        if sandbox_requested and not sandbox_server:
            degraded.append("sandbox_server_package_missing")
        if pii_requested and not pii_dependency:
            # Key redaction remains active; this is a degraded optional PII
            # layer, not a reason to silently disable output protection.
            degraded.append("presidio_missing_keys_only")
        if memory_enabled and not memory_dependency:
            degraded.append("sqlite_vec_missing_memory_disabled")
        model_ready = bool(
            str(self.config.openai_api_key).strip()
            and str(self.config.openai_base_url).strip()
            and str(self.config.openai_model).strip()
        )
        required_ready = model_ready and (not sandbox_requested or sandbox_sdk)
        return {
            "policy_enabled": self.policy.enabled,
            "l2_enabled": self.l2_config.enabled,
            "l5_enabled": self.l5 is not None,
            "pii_active": bool(getattr(self.l5, "pii_active", False)),
            "executor_backend": self.executor_config.backend.value,
            "provenance_enforced": self.provenance_mode,
            "output_egress_guard": self.output_egress_guard_enabled,
            "environment_ready": required_ready,
            "degraded_reasons": degraded,
            "security_dependencies": {
                "sandbox_sdk": sandbox_sdk,
                "sandbox_server_package": sandbox_server,
                "presidio_analyzer": pii_dependency,
                "sqlite_vec": memory_dependency,
                "model_configuration": model_ready,
            },
            "sandbox_requested": sandbox_requested,
            "memory_configured": memory_enabled,
            "pii_requested": pii_requested,
        }


__all__ = [
    "ContextEngine",
    "MemoryEngine",
    "PlanningRuntime",
    "SafetyEngine",
    "SharedCapabilityServices",
    "ToolRuntime",
]
