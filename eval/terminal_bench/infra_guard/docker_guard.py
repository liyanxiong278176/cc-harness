"""Docker daemon, address-pool, and Harbor resource guard."""

from __future__ import annotations

import json
import shutil
from typing import Any

from ._utils import env_flag, run_command
from .base import GuardContext, GuardResult, RecoveryResult


class DockerGuard:
    """Keep Docker healthy without touching unrelated project containers."""

    name = "docker"

    def pre_check(self, context: GuardContext) -> GuardResult:
        del context
        docker = shutil.which("docker")
        if docker is None:
            return GuardResult(self.name, "pre_check", ready=False, blocking=True, error="docker executable is unavailable")
        info = run_command([docker, "info", "--format", "{{json .}}"], timeout=30)
        details: dict[str, Any] = {"docker_info": info}
        if info["returncode"] != 0:
            return GuardResult(self.name, "pre_check", ready=False, blocking=True, details=details, error=info["stderr"][-2_000:])
        try:
            payload = json.loads(info["stdout"].strip() or "{}")
        except ValueError:
            payload = {}
        root = str(payload.get("DockerRootDir") or "")
        server_os = str(payload.get("OSType") or "")
        ready = root in {"", "/var/lib/docker"} and server_os in {"", "linux"}
        if not ready:
            details.update({"docker_root": root, "server_os": server_os})
        counts = self._counts(docker)
        details["counts"] = counts
        warnings: list[str] = []
        if counts.get("networks", 0) > 15:
            warnings.append(f"Docker network count is high: {counts['networks']}")
        if counts.get("stopped_containers", 0) > 10:
            warnings.append(f"stopped container count is high: {counts['stopped_containers']}")
        return GuardResult(
            self.name,
            "pre_check",
            ready=ready,
            blocking=not ready,
            warnings=warnings,
            details=details,
            error=None if ready else "Docker daemon is not the expected native Linux daemon",
        )

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        docker = shutil.which("docker")
        if docker is None:
            return GuardResult(self.name, "prevent", ready=False, blocking=True, error="docker executable is unavailable")
        actions: list[str] = []
        warnings: list[str] = []
        # Cleanup is opt-in at the guard boundary and always scoped to Harbor
        # names/labels.  The official task/verifier remains untouched.
        if env_flag("CC_HARNESS_TERMINAL_GUARD_CLEANUP", True):
            for item in self._owned_stopped_containers(docker):
                result = run_command([docker, "rm", "-f", item], timeout=20)
                if result["returncode"] == 0:
                    actions.append(f"removed stopped Harbor container {item}")
                else:
                    warnings.append(f"could not remove Harbor container {item}: {result['stderr'][-500:]}")
            for item in self._owned_networks(docker):
                result = run_command([docker, "network", "rm", item], timeout=20)
                if result["returncode"] == 0:
                    actions.append(f"removed unused Harbor network {item}")
                else:
                    warnings.append(f"could not remove Harbor network {item}: {result['stderr'][-500:]}")
        return GuardResult(self.name, "prevent", warnings=warnings, actions_taken=actions, details={"cleanup_opt_in": env_flag("CC_HARNESS_TERMINAL_GUARD_CLEANUP", True)})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        text = str(error_text or "").casefold()
        markers = ("address pool", "fully subnetted", "docker daemon", "cannot connect", "no space left")
        if not any(marker in text for marker in markers):
            return RecoveryResult(self.name, recovered=True, details={"skipped": True})
        # ``prevent`` does not use task-specific fields; reuse its scoped,
        # idempotent cleanup implementation without widening the target.
        result = self.prevent(context)
        return RecoveryResult(self.name, recovered=not result.error and not result.warnings, actions_taken=result.actions_taken, details=result.details, error="; ".join(result.warnings) if result.warnings else None)

    def post_cleanup(self, context: GuardContext) -> dict[str, Any]:
        del context
        docker = shutil.which("docker")
        if docker is None:
            return {"ok": False, "error": "docker executable is unavailable"}
        removed = []
        errors = []
        for item in self._owned_networks(docker):
            result = run_command([docker, "network", "rm", item], timeout=20)
            if result["returncode"] == 0:
                removed.append(item)
            else:
                errors.append({"network": item, "error": result["stderr"][-500:]})
        return {"ok": not errors, "removed_unused_harbor_networks": removed, "errors": errors}

    def _counts(self, docker: str) -> dict[str, int]:
        containers = run_command([docker, "ps", "-a", "-q"], timeout=10)
        running = run_command([docker, "ps", "-q"], timeout=10)
        networks = run_command([docker, "network", "ls", "-q"], timeout=10)
        return {
            "containers": len([line for line in containers["stdout"].splitlines() if line.strip()]),
            "running_containers": len([line for line in running["stdout"].splitlines() if line.strip()]),
            "stopped_containers": max(0, len([line for line in containers["stdout"].splitlines() if line.strip()]) - len([line for line in running["stdout"].splitlines() if line.strip()])),
            "networks": len([line for line in networks["stdout"].splitlines() if line.strip()]),
        }

    def _owned_networks(self, docker: str) -> list[str]:
        result = run_command([docker, "network", "ls", "--format", "{{.ID}}\t{{.Name}}"], timeout=10)
        names: list[str] = []
        for line in result["stdout"].splitlines():
            identifier, _, name = line.partition("\t")
            if not identifier or not name:
                continue
            if not any(marker in name.casefold() for marker in ("harbor", "terminal-bench")):
                continue
            inspect = run_command([docker, "network", "inspect", identifier, "--format", "{{json .Containers}}"], timeout=10)
            if inspect["returncode"] == 0 and inspect["stdout"].strip() in {"{}", "null", ""}:
                names.append(identifier)
        return names

    def _owned_stopped_containers(self, docker: str) -> list[str]:
        """Return only stopped containers bearing Harbor/task ownership markers."""

        result = run_command(
            [docker, "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Labels}}\t{{.State}}"],
            timeout=10,
        )
        owned: list[str] = []
        for line in result["stdout"].splitlines():
            identifier, _, remainder = line.partition("\t")
            if not identifier:
                continue
            name, _, tail = remainder.partition("\t")
            labels, _, state = tail.partition("\t")
            haystack = f"{name} {labels}".casefold()
            if any(marker in haystack for marker in ("harbor", "terminal-bench")) and not any(
                word in state.casefold() for word in ("running", "restarting", "paused")
            ):
                owned.append(identifier)
        return owned
