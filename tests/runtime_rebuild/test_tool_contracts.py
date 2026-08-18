from __future__ import annotations

from cc_harness.action_contracts import (
    ActionScheduler,
    RetryDecision,
    ToolContractRegistry,
    ToolRecoveryContract,
)
from cc_harness.run_kernel import ActionRequest
from cc_harness.run_model import EffectClass


def test_first_party_contracts_parallelize_only_read_actions() -> None:
    registry = ToolContractRegistry.first_party()
    scheduler = ActionScheduler(registry)
    actions = [
        ActionRequest("a", "Read", {"path": "a"}, EffectClass.READ_ONLY),
        ActionRequest("b", "Grep", {"pattern": "x"}, EffectClass.READ_ONLY),
        ActionRequest("c", "Write", {"path": "a"}, EffectClass.WORKSPACE_MUTATION),
    ]
    batches = scheduler.batches(actions)
    assert batches[0].parallel is True
    assert len(batches[0].actions) == 2
    assert batches[1].parallel is False


def test_unknown_tool_is_conservative_and_unknown_outcome_is_not_retried() -> None:
    registry = ToolContractRegistry.first_party()
    scheduler = ActionScheduler(registry)
    unknown = registry.get("mcp__server__looks_like_write")
    assert unknown.effect_class is EffectClass.UNKNOWN
    assert scheduler.retry_decision(unknown.tool_name, attempt=1, outcome_unknown=True) is RetryDecision.NO_RETRY
    assert scheduler.requires_approval(ActionRequest("x", unknown.tool_name, {}))


def test_retry_requires_idempotency_and_contract_authority() -> None:
    registry = ToolContractRegistry(
        [
            ToolRecoveryContract(
                "safe-read",
                EffectClass.READ_ONLY,
                retryable=True,
                max_retries=2,
                idempotent=True,
                parallelizable=True,
                requires_approval=False,
            )
        ]
    )
    scheduler = ActionScheduler(registry)
    assert scheduler.retry_decision("safe-read", attempt=1) is RetryDecision.RETRY
    assert scheduler.retry_decision("safe-read", attempt=3) is RetryDecision.NO_RETRY
    assert scheduler.retry_decision("safe-read", attempt=1, outcome_unknown=True) is RetryDecision.NO_RETRY
