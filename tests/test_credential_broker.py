import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from cc_harness.config import SandboxConfig
from cc_harness.credential_broker import CredentialBroker, CredentialBrokerError


def _config() -> SandboxConfig:
    return SandboxConfig(
        vault=True,
        egress_allow=["api.deepseek.com"],
        vault_credentials=[{"name": "deepseek", "env_var": "BROKER_TEST_TOKEN"}],
        vault_bindings=[{
            "name": "deepseek-api",
            "credential": "deepseek",
            "hosts": ["api.deepseek.com"],
            "methods": ["POST"],
            "paths": ["/v1/"],
        }],
    )


@pytest.mark.asyncio
async def test_provision_is_domain_scoped_and_audit_is_secret_free(tmp_path):
    secret = "synthetic-do-not-log"
    vault = MagicMock()
    vault.create = AsyncMock(return_value=MagicMock(revision=7))
    sandbox = MagicMock(credential_vault=vault)
    broker = CredentialBroker(
        _config(),
        tmp_path,
        environ={"BROKER_TEST_TOKEN": secret},
    )

    assert await broker.provision(sandbox) == 7

    payload = vault.create.await_args.kwargs
    assert payload["credentials"][0]["source"]["value"] == secret
    binding = payload["bindings"][0]
    assert binding["match"] == {
        "hosts": ["api.deepseek.com"],
        "schemes": ["https"],
        "methods": ["POST"],
        "paths": ["/v1/"],
    }
    audit = (tmp_path / "logs" / "credential-broker.jsonl").read_text(encoding="utf-8")
    assert secret not in audit
    assert json.loads(audit)["revision"] == 7


@pytest.mark.asyncio
async def test_binding_outside_egress_allowlist_fails_before_vault_call(tmp_path):
    config = _config().model_copy(deep=True)
    config.vault_bindings[0].hosts = ["attacker.example"]
    vault = MagicMock(create=AsyncMock())
    broker = CredentialBroker(
        config,
        tmp_path,
        environ={"BROKER_TEST_TOKEN": "secret"},
    )

    with pytest.raises(CredentialBrokerError, match="outside"):
        await broker.provision(MagicMock(credential_vault=vault))

    vault.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_failure_is_sanitized_and_secret_free(tmp_path):
    secret = "secret-returned-by-bad-provider"
    vault = MagicMock()
    vault.create = AsyncMock(side_effect=RuntimeError(secret))
    broker = CredentialBroker(
        _config(),
        tmp_path,
        environ={"BROKER_TEST_TOKEN": secret},
    )

    with pytest.raises(CredentialBrokerError, match="provisioning failed") as caught:
        await broker.provision(MagicMock(credential_vault=vault))

    assert secret not in str(caught.value)
    assert secret not in (tmp_path / "logs" / "credential-broker.jsonl").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_rotation_uses_revision_and_revoke_clears_state(tmp_path):
    vault = MagicMock()
    vault.create = AsyncMock(return_value=MagicMock(revision=3))
    vault.patch = AsyncMock(return_value=MagicMock(revision=4))
    vault.delete = AsyncMock()
    sandbox = MagicMock(credential_vault=vault)
    broker = CredentialBroker(
        _config(),
        tmp_path,
        environ={"BROKER_TEST_TOKEN": "rotated-secret"},
    )
    await broker.provision(sandbox)

    assert await broker.rotate(sandbox) == 4
    assert vault.patch.await_args.kwargs["expected_revision"] == 3
    await broker.revoke(sandbox)

    vault.delete.assert_awaited_once()
    assert broker.revision is None
