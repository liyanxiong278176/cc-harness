"""Agent workspace and lifecycle guard."""

from __future__ import annotations

from ._utils import write_probe
from .base import GuardContext, GuardResult, RecoveryResult


class AgentGuard:
    name = "agent"

    def pre_check(self, context: GuardContext) -> GuardResult:
        writable, error = write_probe(context.attempt_root)
        return GuardResult(self.name, "pre_check", ready=writable, blocking=True, details={"attempt_root": str(context.attempt_root), "workspace_writable": writable}, error=error)

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"output_format": "cc-harness.print-result.v1", "idle_timeout_is_not_failure": True})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"manual_action_required": True, "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True, "child_process_kill_scope": "attempt-owned-only"}
