"""Fail-closed, sandbox-scoped Credential Vault provisioning."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from cc_harness.config import SandboxConfig, SandboxVaultBinding


class CredentialBrokerError(RuntimeError):
    """Credential material could not be safely provisioned or revoked."""


def _host_is_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for rule in allowed:
        rule = rule.lower().rstrip(".")
        if rule.startswith("*."):
            suffix = rule[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == rule:
            return True
    return False


class CredentialBroker:
    def __init__(
        self,
        config: SandboxConfig,
        project_root: Path,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.project_root = Path(project_root)
        self.environ = os.environ if environ is None else environ
        self.revision: int | None = None

    def _audit(self, action: str, **fields: object) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action,
            **fields,
        }
        path = self.project_root / "logs" / "credential-broker.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except OSError:
            pass

    def _credential_payloads(self) -> list[dict[str, object]]:
        payloads = []
        for item in self.config.vault_credentials:
            value = self.environ.get(item.env_var)
            if not value:
                raise CredentialBrokerError(
                    f"required credential environment variable is unavailable: {item.env_var}"
                )
            payloads.append(
                {"name": item.name, "source": {"type": "inline", "value": value}}
            )
        return payloads

    def _binding_payload(self, item: SandboxVaultBinding) -> dict[str, object]:
        unapproved = [
            host for host in item.hosts
            if not _host_is_allowed(host, self.config.egress_allow)
        ]
        if unapproved:
            raise CredentialBrokerError(
                "credential binding targets are outside the sandbox egress allowlist: "
                + ", ".join(unapproved)
            )
        match: dict[str, object] = {
            "hosts": item.hosts,
            "schemes": item.schemes,
        }
        if item.methods is not None:
            match["methods"] = item.methods
        if item.paths is not None:
            match["paths"] = item.paths
        auth: dict[str, object] = {
            "type": item.auth_type,
            "credential": item.credential,
        }
        if item.header_name is not None:
            auth["name"] = item.header_name
        return {"name": item.name, "match": match, "auth": auth}

    async def provision(self, sandbox) -> int:
        if self.revision is not None:
            raise CredentialBrokerError("credential vault is already provisioned")
        try:
            state = await sandbox.credential_vault.create(
                credentials=self._credential_payloads(),
                bindings=[self._binding_payload(item) for item in self.config.vault_bindings],
            )
        except CredentialBrokerError:
            raise
        except Exception:  # noqa: BLE001 - sanitize arbitrary SDK/provider failures
            self._audit("provision_failed")
            raise CredentialBrokerError("credential vault provisioning failed") from None
        self.revision = int(state.revision)
        self._audit(
            "provisioned",
            revision=self.revision,
            credentials=[item.name for item in self.config.vault_credentials],
            bindings=[item.name for item in self.config.vault_bindings],
        )
        return self.revision

    async def rotate(self, sandbox) -> int:
        if self.revision is None:
            raise CredentialBrokerError("credential vault is not provisioned")
        expected = self.revision
        try:
            state = await sandbox.credential_vault.patch(
                expected_revision=expected,
                credentials={"replace": self._credential_payloads()},
            )
        except Exception:  # noqa: BLE001 - sanitize arbitrary SDK/provider failures
            self._audit("rotation_failed", expected_revision=expected)
            raise CredentialBrokerError("credential vault rotation failed") from None
        self.revision = int(state.revision)
        self._audit("rotated", previous_revision=expected, revision=self.revision)
        return self.revision

    async def revoke(self, sandbox) -> None:
        if self.revision is None:
            return
        revision = self.revision
        try:
            await sandbox.credential_vault.delete()
        except Exception:  # noqa: BLE001 - sandbox destruction still follows
            self._audit("revocation_failed", revision=revision)
            raise CredentialBrokerError("credential vault revocation failed") from None
        self.revision = None
        self._audit("revoked", revision=revision)
