"""Process/fd pressure guard."""

from __future__ import annotations

import os
from pathlib import Path

from .base import GuardContext, GuardResult, RecoveryResult


class ProcessGuard:
    name = "process"

    def pre_check(self, context: GuardContext) -> GuardResult:
        del context
        details: dict[str, object] = {"pid": os.getpid()}
        try:
            details["open_fds"] = len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))
        except OSError:
            details["open_fds"] = None
        try:
            details["process_count"] = len(list(Path("/proc").glob("[0-9]*")))
        except OSError:
            details["process_count"] = None
        return GuardResult(self.name, "pre_check", details=details, warnings=[])

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"kill_scope": "none"})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"manual_action_required": True, "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True, "zombie_reclamation": "none"}
