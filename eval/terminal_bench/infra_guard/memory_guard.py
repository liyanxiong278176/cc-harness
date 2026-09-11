"""Read-only WSL memory and load guard."""

from __future__ import annotations

import os
from pathlib import Path

from .base import GuardContext, GuardResult, RecoveryResult


class MemoryGuard:
    name = "memory"

    def pre_check(self, context: GuardContext) -> GuardResult:
        del context
        meminfo: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                if value.strip().endswith(" kB"):
                    meminfo[key] = int(value.strip()[:-3]) * 1024
        except (OSError, ValueError):
            return GuardResult(self.name, "pre_check", warnings=["/proc/meminfo unavailable"], details={"supported": False})
        available = meminfo.get("MemAvailable", 0)
        total = meminfo.get("MemTotal", 0)
        load = None
        try:
            load = os.getloadavg()[0]
        except (AttributeError, OSError):
            pass
        cpu_count = os.cpu_count() or 1
        details = {"mem_available_bytes": available, "mem_total_bytes": total, "load_1m": load, "cpu_count": cpu_count}
        ready = available >= 512 * 1024**2
        warnings = [] if ready else ["available memory is below 512 MiB"]
        if load is not None and load > cpu_count * 2:
            warnings.append(f"system load is high: {load:.2f}")
        return GuardResult(self.name, "pre_check", ready=ready, blocking=False, warnings=warnings, details=details)

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"destructive_reclamation": False})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"manual_action_required": "inspect WSL/Docker memory before resume", "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True, "reclamation": "read-only"}
