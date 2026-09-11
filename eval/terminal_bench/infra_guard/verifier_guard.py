"""Fail-closed verifier protocol guard."""

from __future__ import annotations

import os

from .base import GuardContext, GuardResult, RecoveryResult


class VerifierGuard:
    name = "verifier"

    def pre_check(self, context: GuardContext) -> GuardResult:
        del context
        forbidden = os.environ.get("CC_HARNESS_TERMINAL_VERIFIER_RUNTIME")
        return GuardResult(self.name, "pre_check", ready=not forbidden, blocking=True, details={"verifier_runtime_override": bool(forbidden), "official_verifier_unchanged": not bool(forbidden)}, error="CC_HARNESS_TERMINAL_VERIFIER_RUNTIME must not be set" if forbidden else None)

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"official_verifier_overlay": False})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"never_replay_model_for_verifier_bootstrap": True, "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True}
