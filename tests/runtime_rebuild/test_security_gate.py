import json

import pytest

from cc_harness.action_contracts import ActionScheduler, ToolContractRegistry
from cc_harness.credential_broker import ActionScopedCapabilityBroker, CredentialBrokerError
from cc_harness.run_kernel import ActionRequest
from cc_harness.security import (
    ProvenanceSource,
    build_action_plan,
    detect_untrusted_echo,
    sanitize_untrusted_output,
)


def test_security_gate_keeps_secrets_out_of_event_safe_capability_records() -> None:
    broker = ActionScopedCapabilityBroker()
    capability = broker.issue(run_id="run", action_id="action", scope=("mail",), secret="TOP-SECRET")
    encoded = json.dumps(capability.__dict__, ensure_ascii=False)
    assert "TOP-SECRET" not in encoded
    with pytest.raises(CredentialBrokerError):
        broker.resolve(capability.capability_id, run_id="other", action_id="action", scope="mail")
    broker.revoke_run("run")
    with pytest.raises(CredentialBrokerError):
        broker.resolve(capability.capability_id, run_id="run", action_id="action", scope="mail")


def test_unknown_tool_is_not_replayed_and_tool_result_is_not_authority() -> None:
    registry = ToolContractRegistry.first_party()
    scheduler = ActionScheduler(registry)
    action = ActionRequest("a", "mcp__unknown__send", {"to": "x"})
    assert registry.get(action.tool_name).effect_class.value == "unknown"
    assert scheduler.retry_decision(action.tool_name, attempt=1, outcome_unknown=True).value == "no_retry"
    plan = build_action_plan(
        action.tool_name,
        {"to": "attacker@example.test"},
        messages=[{"role": "user", "content": "send the report"}],
        tool_result_records=[{"name": "read", "result_full": "attacker@example.test"}],
        capability_metadata={"effect": "external_write"},
    )
    assert plan.fields["to"].source is ProvenanceSource.UNKNOWN
    assert not plan.fields["to"].trusted


def test_untrusted_echo_is_quarantined_not_silently_authorized() -> None:
    finding = detect_untrusted_echo(
        "Booked Safe Hotel; note: Riverside View Hotel.",
        ['{"hotel": "Safe Hotel", "note": "Riverside View Hotel"}'],
        user_text="Book Safe Hotel.",
    )
    assert finding is not None
    assert "Riverside View Hotel" not in sanitize_untrusted_output("Booked Safe Hotel; note: Riverside View Hotel.", finding)
