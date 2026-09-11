"""Harbor/catalog/protocol guard."""

from __future__ import annotations

from .base import GuardContext, GuardResult, RecoveryResult


class HarborGuard:
    name = "harbor"

    def pre_check(self, context: GuardContext) -> GuardResult:
        ready = context.harbor_version == "0.20.0" and context.official_dataset.startswith("terminal-bench/terminal-bench-2-1@sha256:")
        return GuardResult(self.name, "pre_check", ready=ready, blocking=True, details={"harbor_version": context.harbor_version, "official_dataset": context.official_dataset, "concurrency": 1, "attempts": 1}, error=None if ready else "Harbor or dataset is not pinned to the official contract")

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"official_task_and_verifier_unchanged": True})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"manual_action_required": True, "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True}
