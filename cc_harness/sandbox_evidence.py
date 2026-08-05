"""Shared schema constants and digests for sandbox conformance evidence."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

REPORT_SCHEMA = "sandbox.conformance.v2"
GATE_SCHEMA = "sandbox.release-gate.v1"
REQUIRED_PLATFORMS = ("Linux", "Windows")
REQUIRED_TESTS = frozenset({
    "test_sensitive_workspace_paths_are_empty_overlays",
    "test_host_environment_secret_is_not_injected",
    "test_cgroup_cpu_limit_is_enforced",
    "test_cgroup_memory_limit_is_enforced",
    "test_allowlisted_domain_is_reachable",
    "test_unlisted_domain_is_blocked",
    "test_direct_ip_cannot_bypass_domain_policy",
    "test_external_server_requires_and_accepts_matching_attestation",
    "test_allowlisted_private_host_cannot_rebind_around_egress",
    "test_memory_overcommit_is_killed",
    "test_runtime_is_not_privileged_and_has_pid_ceiling",
    "test_dangerous_capabilities_seccomp_and_privilege_gain_are_blocked",
    "test_host_control_sockets_and_host_mounts_are_absent",
    "test_fork_bomb_is_bounded_and_sandbox_remains_usable",
    "test_vault_injects_only_for_the_bound_target",
    "test_vault_rotation_rejects_stale_revision_replay",
    "test_vault_revocation_removes_injection_and_audit_is_redacted",
    "test_background_daemon_cannot_outlive_sandbox_container",
    "test_session_cleanup_removes_sandbox_containers",
})

CONTROL_PATHS = (
    "cc_harness/config.py",
    "cc_harness/credential_broker.py",
    "cc_harness/policy.py",
    "cc_harness/sandbox.py",
    "cc_harness/sandbox_capabilities.py",
    "cc_harness/sandbox_evidence.py",
    "cc_harness/sandbox_release_gate.py",
    "cc_harness/sandbox_server.py",
    "cc_harness/sandbox_workspace.py",
    "scripts/check_sandbox_release_gate.py",
    "scripts/run_sandbox_conformance.py",
    "tests/test_sandbox_conformance.py",
    "tests/test_sandbox_release_gate.py",
    "tests/test_policy.py",
    "sandboxes/Dockerfile",
    "pyproject.toml",
    "uv.lock",
)


def control_bundle_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in CONTROL_PATHS:
        path = Path(root) / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        blob = subprocess.run(
            ["git", "rev-parse", f"HEAD:{relative}"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if blob.returncode == 0:
            digest.update(blob.stdout.strip().encode("ascii"))
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
