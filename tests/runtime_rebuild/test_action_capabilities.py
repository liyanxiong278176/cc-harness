from __future__ import annotations

import pytest

from cc_harness.credential_broker import ActionScopedCapabilityBroker, CredentialBrokerError


def test_action_capability_is_scoped_and_secret_is_not_in_public_record() -> None:
    broker = ActionScopedCapabilityBroker()
    capability = broker.issue(
        run_id="run-1",
        action_id="action-1",
        scope=("github:read",),
        secret="top-secret",
    )
    assert "top-secret" not in repr(capability)
    assert broker.resolve(
        capability.capability_id,
        run_id="run-1",
        action_id="action-1",
        scope="github:read",
    ) == "top-secret"
    with pytest.raises(CredentialBrokerError):
        broker.resolve(
            capability.capability_id,
            run_id="child-run",
            action_id="action-1",
            scope="github:read",
        )


def test_capability_revoke_and_scope_denial() -> None:
    broker = ActionScopedCapabilityBroker()
    capability = broker.issue(run_id="run-1", action_id="a", scope=("read",), secret="s")
    with pytest.raises(CredentialBrokerError):
        broker.resolve(capability.capability_id, run_id="run-1", action_id="a", scope="write")
    broker.revoke(capability.capability_id)
    with pytest.raises(CredentialBrokerError):
        broker.resolve(capability.capability_id, run_id="run-1", action_id="a", scope="read")
