"""Layered, auditable infrastructure guards for Terminal-Bench.

Guards are deliberately diagnostic and scoped.  They never alter the
official task or verifier and never turn a missing verifier execution into a
model failure.  Destructive cleanup is limited to Harbor-owned resources and
is recorded as JSON evidence.
"""

from .base import GuardContext, GuardManager, GuardResult, InfraGuard, RecoveryResult
from .docker_guard import DockerGuard
from .network_guard import NetworkGuard
from .memory_guard import MemoryGuard
from .filesystem_guard import FilesystemGuard
from .dependency_guard import DependencyGuard
from .harbor_guard import HarborGuard
from .agent_guard import AgentGuard
from .verifier_guard import VerifierGuard
from .api_guard import ApiGuard
from .process_guard import ProcessGuard


def build_default_guard_manager(
    *,
    project_root,
    attempt_root,
    task_id: str,
    official_dataset: str,
    harbor_version: str,
) -> GuardManager:
    """Create the one ordered guard chain used by an official trial."""

    context = GuardContext(
        project_root=project_root,
        attempt_root=attempt_root,
        task_id=task_id,
        official_dataset=official_dataset,
        harbor_version=harbor_version,
    )
    return GuardManager(
        context,
        (
            DockerGuard(),
            NetworkGuard(),
            MemoryGuard(),
            FilesystemGuard(),
            DependencyGuard(),
            HarborGuard(),
            AgentGuard(),
            VerifierGuard(),
            ApiGuard(),
            ProcessGuard(),
        ),
    )


__all__ = [
    "AgentGuard",
    "ApiGuard",
    "DockerGuard",
    "DependencyGuard",
    "FilesystemGuard",
    "GuardContext",
    "GuardManager",
    "GuardResult",
    "HarborGuard",
    "InfraGuard",
    "MemoryGuard",
    "NetworkGuard",
    "ProcessGuard",
    "RecoveryResult",
    "VerifierGuard",
    "build_default_guard_manager",
]
