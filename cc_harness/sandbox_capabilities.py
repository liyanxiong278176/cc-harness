"""Machine-readable claims for the current sandbox implementation."""

from __future__ import annotations

from enum import Enum


class CapabilityStatus(str, Enum):
    ENFORCED = "enforced"
    PARTIAL = "partial"
    MISSING = "missing"
    UNVERIFIED = "unverified"


CAPABILITY_PROFILE_SCHEMA = 2
REQUIRED_ISOLATION_CAPABILITIES = (
    "filesystem_scope",
    "process_isolation",
    "resource_limits",
    "network_egress",
    "credential_isolation",
    "command_cancellation",
    "cleanup",
)


def sandbox_capability_profile() -> dict:
    """Return conservative claims derived from currently wired controls."""
    return {
        "schema_version": CAPABILITY_PROFILE_SCHEMA,
        "backend": "opensandbox",
        "security_label": "restricted-preview",
        "isolated_claim_allowed": False,
        "capabilities": {
            "filesystem_scope": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": [
                    "server host-path allowlist is restricted to the project root",
                    "the project root is mounted read-only",
                    "sensitive workspace paths receive nested empty read-only overlays",
                    "Windows and Linux Docker conformance verified empty credential overlays",
                ],
                "blockers": [
                    "nested overlay behavior lacks Kubernetes conformance evidence",
                    "sandbox working-directory behavior lacks conformance evidence",
                ],
            },
            "process_isolation": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": [
                    "owned Docker servers pin dropped capabilities, no-new-privileges and PID limits",
                    "Windows and Linux Docker conformance verified HostConfig, seccomp, privileged syscall denial, fork-bomb bounding and daemon cleanup",
                ],
                "blockers": [
                    "Kubernetes and runtime-daemon-restart escape conformance has not run"
                ],
            },
            "resource_limits": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": [
                    "configured CPU and memory limits are passed to Sandbox.create",
                    "Windows and Linux Docker conformance verified cgroup limits and OOM rejection",
                ],
                "blockers": [
                    "repeated-load resource conformance has not run"
                ],
            },
            "network_egress": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": [
                    "Sandbox.create receives a default-deny domain allowlist policy",
                    "owned servers are configured for dns+nft egress enforcement",
                    "Windows and Linux Docker conformance verified allow, deny and direct-IP blocking",
                    "allowed domains resolving to non-public addresses fail DNS preflight",
                    "external servers require a matching local security configuration attestation",
                ],
                "blockers": [
                    "Kubernetes egress conformance has not run",
                    "DNS preflight cannot eliminate DNS answer changes between validation and use",
                    "remote external servers lack cryptographic runtime attestation",
                ],
            },
            "credential_isolation": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": [
                    "host environment variables are not injected into the sandbox",
                    "workspace credential paths receive empty read-only overlays",
                    "credential proxy activation requires explicit vault opt-in",
                    "Windows and Linux Docker conformance verified host-env and workspace-secret isolation",
                    "Windows and Linux Docker E2E verified scoped injection, wrong-target isolation, revision replay rejection, revocation and redacted audit",
                ],
                "blockers": [
                    "workspace overlay isolation lacks Kubernetes evidence",
                    "credential proxy brokering lacks Kubernetes evidence",
                ],
            },
            "command_cancellation": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": ["command wall timeout destroys the session sandbox"],
                "blockers": ["explicit per-process cancellation conformance has not run"],
            },
            "cleanup": {
                "status": CapabilityStatus.PARTIAL.value,
                "evidence": [
                    "session shutdown and command timeout request sandbox destruction",
                    "Windows and Linux Docker conformance verified runtime and egress container removal",
                    "Windows and Linux Docker conformance verified detached daemon removal",
                ],
                "blockers": [
                    "daemon-restart orphan cleanup conformance has not run"
                ],
            },
        },
        "release_gate": {
            "minimum_consecutive_runs_per_platform": 2,
            "required_platforms": ["Linux", "Windows"],
            "requires_clean_source": True,
            "requires_image_built_in_run": True,
            "max_evidence_age_days": 30,
        },
    }
