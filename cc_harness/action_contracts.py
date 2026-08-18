"""Tool Recovery Contracts and conservative action scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from .run_kernel import ActionRequest
from .run_model import EffectClass, digest_json


class RetryDecision(str, Enum):
    RETRY = "retry"
    NO_RETRY = "no_retry"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class ToolRecoveryContract:
    tool_name: str
    effect_class: EffectClass | str = EffectClass.UNKNOWN
    retryable: bool = False
    max_retries: int = 0
    idempotent: bool = False
    reconcile_supported: bool = False
    cancel_supported: bool = False
    requires_approval: bool = True
    parallelizable: bool = False
    child_allowed: bool = False
    contract_version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("tool name is required")
        if self.max_retries < 0 or self.contract_version < 1:
            raise ValueError("tool contract values are invalid")
        if self.effect_class == EffectClass.UNKNOWN or str(self.effect_class) == EffectClass.UNKNOWN.value:
            if self.retryable or self.parallelizable or self.child_allowed:
                raise ValueError("unknown effect tools cannot opt into optimistic behavior")

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "effect_class": self.effect_class.value
            if isinstance(self.effect_class, EffectClass)
            else self.effect_class,
            "retryable": self.retryable,
            "max_retries": self.max_retries,
            "idempotent": self.idempotent,
            "reconcile_supported": self.reconcile_supported,
            "cancel_supported": self.cancel_supported,
            "requires_approval": self.requires_approval,
            "parallelizable": self.parallelizable,
            "child_allowed": self.child_allowed,
            "contract_version": self.contract_version,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def unknown(cls, tool_name: str, *, source: str = "unknown") -> "ToolRecoveryContract":
        return cls(
            tool_name=tool_name,
            effect_class=EffectClass.UNKNOWN,
            retryable=False,
            max_retries=0,
            idempotent=False,
            reconcile_supported=False,
            cancel_supported=False,
            requires_approval=True,
            parallelizable=False,
            child_allowed=False,
            metadata={"source": source, "conservative": True},
        )


class ToolContractRegistry:
    def __init__(self, contracts: Iterable[ToolRecoveryContract] = ()) -> None:
        self._contracts = {item.tool_name: item for item in contracts}

    def register(self, contract: ToolRecoveryContract) -> None:
        self._contracts[contract.tool_name] = contract

    def get(self, tool_name: str) -> ToolRecoveryContract:
        return self._contracts.get(tool_name, ToolRecoveryContract.unknown(tool_name))

    @classmethod
    def first_party(cls) -> "ToolContractRegistry":
        return cls(
            [
                ToolRecoveryContract(
                    "Read", EffectClass.READ_ONLY, retryable=True, max_retries=2,
                    idempotent=True, parallelizable=True, child_allowed=True, requires_approval=False,
                ),
                ToolRecoveryContract(
                    "Glob", EffectClass.READ_ONLY, retryable=True, max_retries=2,
                    idempotent=True, parallelizable=True, child_allowed=True, requires_approval=False,
                ),
                ToolRecoveryContract(
                    "Grep", EffectClass.READ_ONLY, retryable=True, max_retries=2,
                    idempotent=True, parallelizable=True, child_allowed=True, requires_approval=False,
                ),
                ToolRecoveryContract(
                    "Write", EffectClass.WORKSPACE_MUTATION, retryable=False,
                    requires_approval=False, cancel_supported=False, child_allowed=True,
                ),
                ToolRecoveryContract(
                    "Edit", EffectClass.WORKSPACE_MUTATION, retryable=False,
                    requires_approval=False, cancel_supported=False, child_allowed=True,
                ),
                ToolRecoveryContract(
                    "run_command", EffectClass.UNKNOWN, retryable=False,
                    requires_approval=True, cancel_supported=True,
                ),
            ]
        )

    def from_mcp_metadata(
        self,
        tool_name: str,
        metadata: Mapping[str, Any] | None,
    ) -> ToolRecoveryContract:
        if not metadata or metadata.get("effect") in {None, "unknown"}:
            contract = ToolRecoveryContract.unknown(tool_name, source="mcp_missing_contract")
        else:
            raw_effect = str(metadata["effect"]).strip().lower()
            effect_aliases = {
                "read": EffectClass.READ_ONLY.value,
                "read_only": EffectClass.READ_ONLY.value,
                "write": EffectClass.WORKSPACE_MUTATION.value,
                "local_mutation": EffectClass.WORKSPACE_MUTATION.value,
                "mutation": EffectClass.WORKSPACE_MUTATION.value,
                "external": EffectClass.EXTERNAL_SIDE_EFFECT.value,
                "external_side_effect": EffectClass.EXTERNAL_SIDE_EFFECT.value,
            }
            effect = EffectClass(effect_aliases.get(raw_effect, raw_effect))
            contract = ToolRecoveryContract(
                tool_name=tool_name,
                effect_class=effect,
                retryable=bool(metadata.get("retryable", False)),
                max_retries=int(metadata.get("max_retries", 0)),
                idempotent=bool(metadata.get("idempotent", False)),
                reconcile_supported=bool(metadata.get("reconcile_supported", False)),
                cancel_supported=bool(metadata.get("cancel_supported", False)),
                requires_approval=bool(
                    metadata.get(
                        "requires_approval",
                        metadata.get("requires_user_intent", True),
                    )
                ),
                parallelizable=bool(metadata.get("parallelizable", False)),
                child_allowed=bool(metadata.get("child_allowed", False)),
                metadata={"source": "mcp_contract"},
            )
        self.register(contract)
        return contract


@dataclass(frozen=True)
class ActionBatch:
    actions: tuple[ActionRequest, ...]
    parallel: bool


class ActionScheduler:
    """Schedule only actions justified by their recovery contracts."""

    def __init__(self, registry: ToolContractRegistry) -> None:
        self.registry = registry

    def batches(self, actions: Iterable[ActionRequest]) -> tuple[ActionBatch, ...]:
        batches: list[ActionBatch] = []
        read_batch: list[ActionRequest] = []
        for action in actions:
            contract = self.registry.get(action.tool_name)
            effect = contract.effect_class
            if (
                effect == EffectClass.READ_ONLY
                and contract.parallelizable
                and not action.requires_approval
            ):
                read_batch.append(action)
                continue
            if read_batch:
                batches.append(ActionBatch(tuple(read_batch), parallel=True))
                read_batch = []
            batches.append(ActionBatch((action,), parallel=False))
        if read_batch:
            batches.append(ActionBatch(tuple(read_batch), parallel=True))
        return tuple(batches)

    def retry_decision(self, tool_name: str, *, attempt: int, outcome_unknown: bool = False) -> RetryDecision:
        contract = self.registry.get(tool_name)
        if outcome_unknown:
            return RetryDecision.RECONCILE if contract.reconcile_supported else RetryDecision.NO_RETRY
        if contract.retryable and contract.idempotent and attempt <= contract.max_retries:
            return RetryDecision.RETRY
        return RetryDecision.NO_RETRY

    def requires_approval(self, action: ActionRequest) -> bool:
        return action.requires_approval or self.registry.get(action.tool_name).requires_approval

    def can_cancel(self, tool_name: str) -> bool:
        return self.registry.get(tool_name).cancel_supported


__all__ = [
    "ActionBatch",
    "ActionScheduler",
    "RetryDecision",
    "ToolContractRegistry",
    "ToolRecoveryContract",
]
